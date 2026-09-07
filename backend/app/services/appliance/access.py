"""Which remote doors still admit the operator (#1013).

The appliance has two independent source restrictions, each of which can
close a way in:

  * the **Web UI** allow-list (``platform_settings.web_ui_allowed_cidrs``,
    #285 Phase 6) — nftables :80/:443 on every node plus
    ``loadBalancerSourceRanges`` on the MetalLB VIP;
  * the **SSH** allow-list (``ssh_allowed_source_networks`` in force only
    under ``ssh_lockdown``, #1009) — the nft drop-in that scopes the sshd
    port, once the port-22 floor is retired.

Each shipped with its own anti-lockout guard, and each guard could only see
its own door. So an operator could pass both, one at a time — scope the Web
UI to the management network, then scope SSH to the same one — and end up
reachable through neither, with only the console left. Nothing at any point
said the other door was also closing, because nothing could.

This module is the thing both write paths call, so they cannot disagree
about the answer. It is pure: no session, no request, no settings write.
Callers pass the state that would result from the change they are about to
make, and get back a per-door verdict plus the one question that matters —
:attr:`AccessReport.console_only`.

WHAT COUNTS AS A DOOR. Only a *remote* path governed by a source
restriction. The console is not a door here: it is the floor the escalation
warns you are being left with. SSH being unusable for a reason other than
its allow-list (password auth off with no authorized keys) is not modelled
either, because :func:`app.services.appliance.ssh.validate_lockout_safe`
refuses that combination outright — it cannot be a resulting state.

THE AMBIGUOUS CASE, AND WHY IT RESOLVES THE WAY IT DOES. Before this module
the two guards scored an unreadable source address in **opposite**
directions: the Web UI one counted it as *not covered* (warn), the SSH one
as *covered* (proceed). Both had a written rationale, and two guards doing
the same job on the same box disagreeing about the same input is not
defensible as a pair. :func:`covers` settles it one way — an address we
cannot read is **not** covered — for three reasons:

  1. The costs are asymmetric. A warning the operator did not need costs a
     checkbox; a warning they needed and did not get costs a trip to the
     console, or on a VM with no console at all, a rebuild.
  2. #1009's argument for the other direction was that a gate blocking on
     "I could not tell" is one operators force past by reflex. That is an
     argument about frequency, and the frequency here is near zero:
     ``get_trusted_client_ip`` falls back to the ASGI peer address, which
     is populated for every real HTTP request. It is absent only where
     there is no client at all — which is not a shape in which an operator
     has a session to be locked out of.
  3. It makes the conservative choice the *same* choice in both the
     single-door guards and the console-only escalation, so there is one
     rule to state rather than three.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.models.settings import PlatformSettings

#: Door identifiers. Stable strings — they reach the API response and the UI.
WEB_UI = "web_ui"
SSH = "ssh"


def covers(address: str | None, cidrs: Iterable[str]) -> bool:
    """Is ``address`` inside any of ``cidrs``?

    The ONE implementation of that question (it was two, differing in the
    ambiguous case — see the module docstring). Everything about the answer
    is deliberate:

    * A missing or unparseable ``address`` is **not** covered. We cannot
      confirm the door admits it, and an unconfirmable door is not a door.
    * A malformed entry in ``cidrs`` is skipped rather than fatal. Both
      lists are validated on the way in, and a bad entry cannot make the
      caller covered — failing the whole check on one would turn a stale
      row into a lockout warning on every save.
    * Families do not cross: an IPv6 caller is not covered by an IPv4-only
      list, which is what nftables will do with the same pair.
    """
    if not address:
        return False
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    for entry in cidrs:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except (ValueError, TypeError):
            continue
        if addr.version == net.version and addr in net:
            return True
    return False


@dataclass(frozen=True)
class Door:
    """One remote path, and whether it would still let ``caller_ip`` in."""

    name: str
    #: Is a source restriction in force for this door right now?
    restricted: bool
    #: The networks it is scoped to (empty when unrestricted).
    allowed_cidrs: tuple[str, ...]
    #: Would the caller reach it? Always True when unrestricted.
    admits: bool


@dataclass(frozen=True)
class AccessReport:
    caller_ip: str | None
    web_ui: Door
    ssh: Door

    @property
    def doors(self) -> tuple[Door, ...]:
        return (self.web_ui, self.ssh)

    @property
    def admitting(self) -> tuple[str, ...]:
        """Names of the doors that would still admit the caller."""
        return tuple(d.name for d in self.doors if d.admits)

    @property
    def console_only(self) -> bool:
        """No remote door would admit the caller — the console is all that
        is left. This is the single question both write paths escalate on."""
        return not self.admitting


def _door(name: str, cidrs: Sequence[str], caller_ip: str | None) -> Door:
    # Blank entries are dropped, so a list of nothing but whitespace reads as
    # unrestricted — which suppresses a warning rather than raising a false
    # one. Both lists are CIDR-validated on write, so that state is not
    # reachable through the API; this only keeps the report total.
    scoped = [str(c).strip() for c in cidrs if str(c).strip()]
    if not scoped:
        # Unrestricted. Open to every source, so it admits the caller even
        # when we could not read the caller's address at all.
        return Door(name=name, restricted=False, allowed_cidrs=(), admits=True)
    return Door(
        name=name,
        restricted=True,
        allowed_cidrs=tuple(scoped),
        admits=covers(caller_ip, scoped),
    )


def effective_doors(
    settings: PlatformSettings | None,
    caller_ip: str | None,
    *,
    web_ui_cidrs: Sequence[str] | None = None,
    ssh_lockdown: bool | None = None,
    ssh_cidrs: Sequence[str] | None = None,
) -> AccessReport:
    """Build the door report for ``caller_ip``.

    Each keyword overrides the corresponding stored value, so a write path
    can ask about the state its request WOULD produce rather than the one on
    disk. ``None`` means "use what is stored" — unambiguous, because a
    cleared list is ``[]`` and a cleared flag is ``False``.

    The SSH door mirrors :func:`app.services.appliance.ssh.effective_ssh_scope`:
    a configured allow-list restricts nothing until ``ssh_lockdown`` is on,
    and lockdown with an empty list is refused at the PUT, so "restricted"
    here means exactly "lockdown on AND a non-empty scope".
    """
    stored_web = list(settings.web_ui_allowed_cidrs or []) if settings else []
    stored_lockdown = bool(settings.ssh_lockdown) if settings else False
    stored_ssh = list(settings.ssh_allowed_source_networks or []) if settings else []

    web = list(web_ui_cidrs) if web_ui_cidrs is not None else stored_web
    lockdown = stored_lockdown if ssh_lockdown is None else bool(ssh_lockdown)
    ssh_scope = list(ssh_cidrs) if ssh_cidrs is not None else stored_ssh

    return AccessReport(
        caller_ip=caller_ip,
        web_ui=_door(WEB_UI, web, caller_ip),
        ssh=_door(SSH, ssh_scope if lockdown else [], caller_ip),
    )


#: The 422 an operator sees when the change they are making would leave the
#: console as the only way in. Deliberately NOT satisfied by either
#: per-setting override: those acknowledge losing one door while the other
#: stays open, which is a materially smaller thing to accept, and an
#: operator may have ticked one for an unrelated reason.
CONSOLE_ONLY_DETAIL = (
    "Refusing to close the last remote way in. After this change neither the "
    "Web UI source restriction nor the SSH source restriction would admit the "
    "address you are connecting from ({caller}). Unless you can reach one of "
    "those networks another way, the appliance console becomes the only way "
    "into this fleet — and a VM with no console attached would need a "
    "rebuild. Web UI allows {web}; SSH allows {ssh}. Add your network to one "
    "of them, or re-send with acknowledge_console_only=true."
)

#: What these guards can and cannot honestly assert.
#:
#: Every verdict here is about ONE address: the source of the HTTP request
#: being made. That is exactly the right address for the Web UI door — the
#: operator is using it, over that door, right now — and it is only a PROXY
#: for the SSH door, because browsing from one network and SSHing from
#: another is legitimate (#1009 says so where it justifies the per-door
#: warning being advisory rather than a refusal).
#:
#: So the copy is asymmetric on purpose. A message may state that the SSH
#: list *includes the address you are connecting from* — a fact — but not
#: that you will therefore be able to SSH, which needs an assumption about
#: where you SSH from. Going the other way, "you can still reach this UI" is
#: sound and is stated plainly. The escalation names the address and the
#: assumption rather than asserting the consequence outright, which keeps it
#: honest without softening it: it fires on the cautious side either way,
#: since it is an acknowledgeable warning and not a refusal.
SSH_REACHABILITY_CAVEAT = (
    "which includes the address you are connecting from now (you may SSH " "from elsewhere)"
)


def console_only_detail(report: AccessReport) -> str:
    """Render :data:`CONSOLE_ONLY_DETAIL` for ``report``.

    Both write paths raise the SAME text, because it describes one state of
    the appliance rather than one setting — an operator who meets it from
    the SSH screen and an operator who meets it from the firewall screen are
    being told about the same two lists.
    """

    def _fmt(door: Door) -> str:
        # A door that is not restricted always admits, so a console-only
        # report has both doors restricted and this arm is unreachable from
        # the escalation path. It exists so the renderer stays total for a
        # caller that formats a report for some other reason.
        if not door.restricted:
            return "any source"
        return ", ".join(door.allowed_cidrs)

    return CONSOLE_ONLY_DETAIL.format(
        caller=report.caller_ip or "unknown",
        web=_fmt(report.web_ui),
        ssh=_fmt(report.ssh),
    )

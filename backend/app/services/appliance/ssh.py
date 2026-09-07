"""Issue #157 — appliance SSH authorized_keys + sshd hardening.

Source-of-truth lives on ``platform_settings`` (singleton); this module
renders an operator-managed ``~admin/.ssh/authorized_keys`` file + an
``/etc/ssh/sshd_config.d/spatiumddi.conf`` drop-in from those columns, and
folds the rendered text into the DNS + DHCP ConfigBundle long-poll + the
supervisor heartbeat so every appliance host (local + remote agents) picks
it up the same way #153 (SNMP) / #154 (NTP) / #343 (LLDP) / #156 (syslog)
distribute their host config.

Design notes:

* sshd runs at the Debian host level on the appliance image, NOT in a
  container. The trigger-file → host systemd path-unit pattern lets the
  api / supervisor ship config without itself being on the host.
* The appliance admin user is ``admin``; keys land in
  ``~admin/.ssh/authorized_keys`` (the runner writes mode 0600, dir 0700,
  owned by admin). Public keys are NOT secrets — they are stored verbatim
  in the JSONB column (no Fernet, no redaction), unlike the SNMP community
  / syslog CA PEM.
* ``PasswordAuthentication`` defaults on so an existing field install does
  not lose password login on upgrade. Disabling it with zero authorized
  keys would lock the operator out, so :func:`validate_lockout_safe`
  refuses that combination — enforced both on the settings PUT and again
  defensively on the host runner.
* ``Port`` may be changed, but a management floor opens ``tcp dport 22``
  regardless — baked at ``/etc/nftables.d/00-spatium-ssh.nft`` and emitted
  again by the firewall renderers (``firewall.py`` / ``firewall_merge.py``
  / the supervisor's) — so even a bad port change leaves 22 open as the
  escape hatch. The host nft drop-in opens the configured port,
  SOURCE-SCOPED to ``ssh_allowed_source_networks`` (sshd has no native
  source-CIDR filter — this DIVERGES from the SNMP / NTP drop-ins which
  open unscoped).
* ``ssh_lockdown`` (#1009) is what turns that scope into enforcement, and
  :func:`effective_ssh_scope` is the ONE place that resolves it. nftables
  is first-match-wins and the floor sorts first, so until the floor is
  retired the scoped rule is dead code on port 22 — the allowlist rendered
  perfectly and restricted nothing, while the UI, the applied sidecar and
  the nft dry-run all reported success. Retiring the floor removes what
  ``docs/design/FLEET_FIREWALL.md`` §6.1 calls the irreducible recovery
  channel, so it is an explicit switch rather than a consequence of typing
  a CIDR. With it off the effective scope is EMPTY, which every consumer
  already reads as "open unconditionally" — so an older host runner that
  has never heard of lockdown does the safe thing by construction.
* ``ssh_bundle`` returns a STABLE dict shape even when "disabled"
  (here "disabled" = the default state: password auth on, no managed
  keys) so every hash-compare caller (DHCP-agent ETag mix, supervisor
  heartbeat ``maybe_fire_ssh_reload``) reads the same keys without a
  KeyError — same contract the SNMP / NTP / LLDP / syslog bundles follow.
* Empty / disabled settings still produce a deterministic body so the
  host-side runner can compare byte-for-byte against the on-disk file and
  skip the reload when nothing changed.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

from app.models.settings import PlatformSettings

# OpenSSH public-key type prefixes we accept. Covers the common modern
# key algorithms operators paste; anything outside this set is rejected
# at the settings-PUT layer (and re-checked here for the bundle).
_VALID_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ssh-dss",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)

# A single base64 token (the key blob). OpenSSH base64 is the standard
# alphabet plus ``=`` padding; reject anything else so a line with
# embedded whitespace / control chars / a smuggled second key can't slip
# through into authorized_keys.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# Comment field — free-form label after the key blob. Permit printable
# ASCII minus control chars; reject newlines / NULs explicitly (a
# newline would let a hostile comment inject a second authorized_keys
# line). Empty comment is fine.
_COMMENT_RE = re.compile(r"^[\x20-\x7e]*$")


def is_valid_public_key(public_key: str) -> bool:
    """Return True if ``public_key`` is a well-formed single OpenSSH public
    key line (``<type> <base64-blob> [comment]``).

    Strict by design — this guards what lands in authorized_keys, so a
    malformed / multi-line / control-char-bearing value must be rejected
    rather than silently written (a smuggled newline could inject an extra
    key). Used by the settings-router field validator and re-applied here
    so the renderer never emits a garbage line.
    """
    if not isinstance(public_key, str):
        return False
    # No embedded control chars / newlines anywhere in the line — a smuggled
    # newline could inject an extra authorized_keys entry.
    if any(ord(c) < 0x20 for c in public_key):
        return False
    parts = public_key.strip().split()
    if len(parts) < 2:
        return False
    key_type, blob = parts[0], parts[1]
    if key_type not in _VALID_KEY_TYPES:
        return False
    if not _BASE64_RE.match(blob):
        return False
    # The blob must decode as base64 AND its embedded length-prefixed
    # algorithm name must match the declared key type — OpenSSH encodes
    # the type as the first string inside the blob.
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    if len(raw) < 4:
        return False
    name_len = int.from_bytes(raw[:4], "big")
    if name_len <= 0 or name_len > len(raw) - 4:
        return False
    embedded_type = raw[4 : 4 + name_len].decode("ascii", errors="replace")
    if embedded_type != key_type:
        return False
    # Comment (everything after the blob) must be printable, no control.
    comment = " ".join(parts[2:])
    if comment and not _COMMENT_RE.match(comment):
        return False
    return True


def key_fingerprint(public_key: str) -> str | None:
    """Return the OpenSSH SHA256 fingerprint (``SHA256:<b64>``) of a public
    key, or ``None`` if it can't be parsed. Public — fingerprints are
    safe to surface (e.g. the ``find_ssh_settings`` MCP tool)."""
    parts = public_key.strip().split()
    if len(parts) < 2:
        return None
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    digest = hashlib.sha256(raw).digest()
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


def validate_lockout_safe(keys: list[dict[str, Any]], password_auth_enabled: bool) -> bool:
    """Lockout-safety guard (#157).

    Returns True if the resulting SSH config still leaves at least one way
    in: either password auth stays enabled, OR at least one valid
    authorized key survives. Disabling password auth with zero usable keys
    would lock the operator out of every appliance host, so the PUT (and
    the host runner, defensively) refuse that combination.

    A key only counts if it is a well-formed public key — a list full of
    garbage entries doesn't save you.
    """
    if password_auth_enabled:
        return True
    return any(
        is_valid_public_key(str(k.get("public_key") or "")) for k in keys if isinstance(k, dict)
    )


def _normalise_key(entry: Any) -> dict[str, str] | None:
    """Coerce a stored authorized-key entry into ``{name, public_key,
    comment}`` strings; drop anything that isn't a well-formed key."""
    if not isinstance(entry, dict):
        return None
    public_key = str(entry.get("public_key") or "").strip()
    if not is_valid_public_key(public_key):
        return None
    return {
        "name": str(entry.get("name") or "").strip(),
        "public_key": public_key,
        "comment": str(entry.get("comment") or "").strip(),
    }


def render_authorized_keys(settings: PlatformSettings) -> str:
    """Return the full ``~admin/.ssh/authorized_keys`` body.

    Deterministic — same settings → same bytes — so the host-side runner's
    config-hash idempotency check stays stable across long-poll cycles.
    One key per line; a leading ``# Managed by SpatiumDDI`` banner. The
    per-key ``name`` / ``comment`` are folded into a single trailing
    comment on each line so an operator inspecting the file sees which
    managed entry it is.
    """
    lines: list[str] = [
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.",
        "# Source of truth: /appliance → SSH in the SpatiumDDI UI.",
    ]
    for entry in settings.ssh_authorized_keys or []:
        key = _normalise_key(entry)
        if key is None:
            continue
        # The public_key already carries an optional inline comment; if
        # the operator gave a name/comment, append it as a managed tag so
        # the line is identifiable without leaking anything (public).
        tag_bits = [b for b in (key["name"], key["comment"]) if b]
        tag = f"  # spatium:{' / '.join(tag_bits)}" if tag_bits else ""
        lines.append(f"{key['public_key']}{tag}")
    return "\n".join(lines) + "\n"


def _password_auth_on(settings: PlatformSettings) -> bool:
    """Read the password-auth flag, treating ``None`` as the documented
    default (ON). SQLAlchemy column ``default=True`` only fires on INSERT
    flush, so a freshly-constructed / not-yet-flushed singleton row reads
    ``None`` — without this the renderer would emit ``no`` and mistakenly
    look like an operator disabled password auth (lockout-safety footgun)."""
    val = settings.ssh_password_auth_enabled
    if val is None:
        return True
    return bool(val)


def render_sshd_config(settings: PlatformSettings) -> str:
    """Return the full ``/etc/ssh/sshd_config.d/spatiumddi.conf`` drop-in.

    Emits only the three hardening directives SpatiumDDI manages —
    ``Port``, ``PasswordAuthentication``, ``PermitRootLogin`` — so the rest
    of the host sshd config is left to the image baseline. Deterministic.
    """
    port = int(settings.ssh_port or 22)
    password_auth = "yes" if _password_auth_on(settings) else "no"
    permit_root = "yes" if bool(settings.ssh_allow_root_login) else "no"
    return (
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.\n"
        "# Source of truth: /appliance → SSH in the SpatiumDDI UI.\n"
        f"Port {port}\n"
        f"PasswordAuthentication {password_auth}\n"
        f"PermitRootLogin {permit_root}\n"
    )


def _valid_key_count(settings: PlatformSettings) -> int:
    """Count the well-formed authorized keys (what the renderer emits)."""
    return sum(
        1 for entry in (settings.ssh_authorized_keys or []) if _normalise_key(entry) is not None
    )


def effective_ssh_scope(settings: PlatformSettings) -> list[str]:
    """The source CIDRs the host firewall should actually enforce.

    ``ssh_allowed_source_networks`` is what the operator typed;
    ``ssh_lockdown`` is whether it is in force. Everything downstream — the
    ssh bundle the host runner renders ``50-spatium-ssh.nft`` from, and the
    three firewall renderers that decide whether to retire the port-22
    floor — reads THIS, so the two halves can never disagree about whether
    SSH is restricted (#1009).

    Empty means "open unconditionally", which is exactly what every existing
    consumer already does with an empty list — so lockdown-off is
    indistinguishable from never having configured a scope, including to an
    older host runner that predates the flag.
    """
    if not settings.ssh_lockdown:
        return []
    return list(settings.ssh_allowed_source_networks or [])


def ssh_bundle(settings: PlatformSettings) -> dict[str, Any]:
    """Build the ``ssh_settings`` block shipped to agents + supervisor.

    Returns a STABLE dict shape so every hash-compare caller (DHCP-agent
    ConfigBundle ETag mix, supervisor heartbeat ``maybe_fire_ssh_reload``)
    reads the same keys without a KeyError:

      * ``enabled`` — True unless this is the pristine default state
        (password auth on + no managed keys). The host runner uses this
        to decide between applying the drop-in and tearing it back down.
      * ``config_hash`` — sha256 over every input the host runner renders
        from: the two file bodies PLUS the ssh port and the effective source
        scope (see below — the scope reaches neither body, and omitting it
        meant a lockdown toggle never re-fired the runner). Hashed so
        any change to either body shifts the agent/supervisor ETag.
      * ``authorized_keys`` — the rendered authorized_keys body (always
        rendered so a disable still ships an empty-keys body the runner
        can write).
      * ``sshd_conf`` — the rendered sshd drop-in body.
      * ``ssh_port`` — int, for the host nft drop-in.
      * ``allowed_source_networks`` — the EFFECTIVE scope from
        :func:`effective_ssh_scope`, i.e. empty unless ``ssh_lockdown``
        is on (empty = open unconditionally).
      * ``password_auth`` — bool, surfaced so the runner can apply the
        lockout guard defensively.
      * ``key_count`` — count of well-formed keys the runner is expected
        to write (the supervisor reports the ACTUALLY-applied count back
        per-host; this is the rendered expectation).
    """
    authorized_keys = render_authorized_keys(settings)
    sshd_conf = render_sshd_config(settings)
    key_count = _valid_key_count(settings)
    password_auth = _password_auth_on(settings)
    # "Disabled" here = the pristine default: password auth on AND no
    # managed keys AND default port AND root login off. In that state we
    # ship an empty-marker block so the runner tears its drop-in down and
    # leaves the host's baseline sshd config untouched.
    is_default = (
        password_auth
        and key_count == 0
        and int(settings.ssh_port or 22) == 22
        and not bool(settings.ssh_allow_root_login)
        # The typed list, not the effective scope: a configured-but-not-
        # enforced allowlist is still operator config, and collapsing it
        # into "default" here would tear the drop-in down and lose the
        # port/keys state the runner is managing.
        and not list(settings.ssh_allowed_source_networks or [])
    )
    enabled = not is_default
    body = authorized_keys + sshd_conf
    # The hash is the ONLY thing that re-fires the host runner
    # (``_fire_host_config`` short-circuits on an unchanged one), so it has to
    # cover every input the runner renders from — not just the two file bodies
    # it happens to be built out of.
    #
    # ``allowed_source_networks`` appears in NEITHER body: it is an nftables
    # scope, not an sshd directive. Before #1009 that was harmless, because
    # the drop-in it feeds was dead code on port 22 anyway. It is not harmless
    # now: toggling ``ssh_lockdown`` changes the effective scope and nothing
    # else, so the hash would be unchanged, the trigger would not fire, and
    # ``50-spatium-ssh.nft`` would keep its UNCONDITIONAL accept — which sorts
    # ahead of the scoped management line — while the firewall plane had
    # already retired the port-22 floor. SSH open from anywhere, every surface
    # reporting it restricted: the exact #1009 bug, re-created one layer down.
    #
    # The port is folded in defensively. It does reach ``sshd_conf`` today, so
    # this is belt-and-braces rather than a fix — but it is the other value
    # the nft drop-in renders from, and the coupling that makes it safe to
    # omit is not one this function states anywhere.
    #
    # Consequence, and it is intended: the formula changed, so every install
    # sees one hash change on upgrade and re-fires the trigger once. The apply
    # is idempotent and the runner compares byte-for-byte, so the cost is one
    # no-op reload.
    hash_input = "\n".join(
        [
            body,
            f"port={int(settings.ssh_port or 22)}",
            "scope=" + ",".join(effective_ssh_scope(settings)),
        ]
    )
    config_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest() if enabled else ""
    return {
        "enabled": enabled,
        "config_hash": config_hash,
        "authorized_keys": authorized_keys,
        "sshd_conf": sshd_conf,
        "ssh_port": int(settings.ssh_port or 22),
        "allowed_source_networks": effective_ssh_scope(settings),
        "password_auth": password_auth,
        "key_count": key_count,
    }

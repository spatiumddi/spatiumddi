"""Issue #153 — appliance SNMP support.

Source-of-truth lives on ``platform_settings`` (singleton); this
module renders an ``/etc/snmp/snmpd.conf`` payload from those columns
and folds the rendered text into the DNS + DHCP ConfigBundle long-
poll so every appliance host (local + remote agents) picks it up
the same way Phase 8f-4 ships slot-upgrade triggers.

Design notes:

* snmpd runs at the OS level on the appliance image, NOT in a
  container — HOST-RESOURCES-MIB needs unfiltered ``/proc`` and
  ``/sys`` which a containerised snmpd can't see without making
  the container effectively privileged. The trigger-file → host
  systemd path-unit pattern lets the api container ship config
  without itself being on the host.
* v2c uses ``rocommunity`` per source CIDR — net-snmp's shorthand
  that expands to a ``com2sec`` / ``group`` / ``access`` triple
  with the source filter baked in. Cleaner config and snmpd does
  the source filtering itself rather than relying on nftables.
* v3 uses the ``createUser`` mechanism. snmpd reads createUser
  lines on first start, hashes the passwords against its
  ``engineID``, and rewrites the result into
  ``/var/lib/snmp/snmpd.conf``. The host-side runner wipes that
  file before reload so each rendered config gets a clean run-
  through; this rotates the engineID on every config change,
  which is acceptable for a first cut. A future iteration could
  persist the engineID and compute hashed ``usmUser`` lines
  server-side.
* Empty / disabled settings still produce a *valid* snmpd.conf
  body (just an inert daemon with no community / users) so the
  runner can compare it byte-for-byte against the on-disk file
  and skip the reload when nothing changed.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.crypto import decrypt_str
from app.models.settings import PlatformSettings

# The first cut emits IPv4 + IPv6 listeners on the standard SNMP UDP
# port. Operators who want to bind to a specific interface can
# subclass this in a follow-up — global default is good enough for
# the appliance shape.
_AGENT_ADDRESS_LINE = "agentAddress udp:161,udp6:[::1]:161"

# net-snmp Debian ships a default that loads HOST-RESOURCES + DISMAN
# implicitly; we don't have to ``load`` them explicitly. The lines
# below are the minimum operator-meaningful config — sysContact /
# sysLocation / view / agentAddress + the community or user table.


def render_snmpd_conf(settings: PlatformSettings) -> str:
    """Return the full ``/etc/snmp/snmpd.conf`` body for these settings.

    Returns a stable, deterministic string — same settings → same
    bytes, so the host-side runner can compare against the on-disk
    file and skip the reload when nothing changed.
    """
    lines: list[str] = [
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.",
        "# Source of truth: Settings → Appliance → SNMP in the SpatiumDDI UI.",
        "",
    ]

    # Common headers — these are safe to emit even when SNMP is
    # globally disabled. The runner won't reach this code in that
    # case (it skips activation when ``snmp_enabled`` is false), but
    # the renderer staying pure lets callers test the output shape.
    sys_contact = (settings.snmp_sys_contact or "").strip()
    sys_location = (settings.snmp_sys_location or "").strip()
    if sys_contact:
        lines.append(f"sysContact     {_quote(sys_contact)}")
    if sys_location:
        lines.append(f"sysLocation    {_quote(sys_location)}")
    lines.append(_AGENT_ADDRESS_LINE)
    lines.append("")

    # View that exposes the whole MIB tree to authorised queries.
    # Per-OID restrictions are out of scope for the first cut.
    lines.append("view all included .1")
    lines.append("")

    version = (settings.snmp_version or "v2c").strip()
    sources = list(settings.snmp_allowed_sources or [])

    if version == "v2c":
        lines.extend(_render_v2c(settings, sources))
    elif version == "v3":
        lines.extend(_render_v3(settings, sources))

    # Trailing newline so editors don't add a "no newline at end of
    # file" diff when an operator inspects the file directly.
    return "\n".join(lines) + "\n"


def _quote(s: str) -> str:
    """net-snmp doesn't have a formal quoting grammar; whitespace and
    single quotes are the practical breakers. Use double quotes and
    escape any double quote in the value."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_v2c(settings: PlatformSettings, sources: list[str]) -> list[str]:
    """Emit one ``rocommunity`` (and ``rocommunity6`` for v6 sources)
    line per allowed source CIDR. No allowed sources = no usable
    config (snmpd will be running but refuse every query); that's
    explicit by operator choice."""
    community = _decrypt_or_none(settings.snmp_community_encrypted)
    if not community:
        return ["# SNMP v2c enabled but no community configured — daemon will accept nothing."]
    if not sources:
        return [
            f"# SNMP v2c community {_quote(community)} configured but no allowed",
            "# sources — daemon will accept nothing. Add at least one CIDR in",
            "# Settings → Appliance → SNMP.",
        ]

    out: list[str] = []
    for src in sources:
        # net-snmp distinguishes v4 vs v6 by directive name; we pick
        # based on whether the CIDR contains a colon. ``ip_network``
        # validation in the router already canonicalised the entry.
        directive = "rocommunity6" if ":" in src else "rocommunity"
        out.append(f"{directive} {community} {src}")
    return out


def _render_v3(settings: PlatformSettings, sources: list[str]) -> list[str]:
    """Emit one ``createUser`` line per configured v3 user plus a
    matching ``rouser`` granting read access to the full view.

    ``createUser`` is consumed by snmpd on first start; the host-side
    runner wipes ``/var/lib/snmp/snmpd.conf`` before reload so this
    pass runs fresh each time.
    """
    raw_users: list[dict[str, Any]] = list(settings.snmp_v3_users or [])
    if not raw_users:
        return ["# SNMP v3 enabled but no users configured — daemon will accept nothing."]

    # v3 source filters are implemented per-rouser via the OID arg's
    # source-filter sibling — net-snmp's ``rouser`` itself doesn't
    # carry a CIDR arg, but ``access`` does. For the first cut, if
    # ``snmp_allowed_sources`` is set we add a single ``com2sec6`` /
    # ``com2sec`` line per source linking to a dummy community-name
    # context so the v3 access table inherits the filter. If empty,
    # v3 users are reachable from any source the OS firewall lets in.
    out: list[str] = []
    for u in raw_users:
        username = (u.get("username") or "").strip()
        if not username:
            continue
        auth_proto = u.get("auth_protocol") or "none"
        priv_proto = u.get("priv_protocol") or "none"
        auth_pass = _decrypt_v3_pass(u.get("auth_pass_enc"))
        priv_pass = _decrypt_v3_pass(u.get("priv_pass_enc"))

        # Build the createUser line. Each arg is required-or-not
        # based on the security level the user expects.
        create_parts: list[str] = ["createUser", username]
        access_level = "noauth"
        if auth_proto != "none" and auth_pass:
            create_parts.extend([auth_proto, _quote(auth_pass)])
            access_level = "auth"
            if priv_proto != "none" and priv_pass:
                create_parts.extend([priv_proto, _quote(priv_pass)])
                access_level = "priv"
        out.append(" ".join(create_parts))
        out.append(f"rouser {username} {access_level} .1")

    if sources:
        # Inert reminder — first cut doesn't restrict v3 by source
        # CIDR. snmpd's USM model authenticates by user+pass, not
        # source IP; operators wanting source filtering on top of
        # USM should add a host firewall rule. Surface this clearly
        # so they don't think the field is silently honoured.
        out.append("")
        out.append("# Note: SNMP v3 ignores snmp_allowed_sources — USM authenticates by")
        out.append("# user+password, not source IP. Use the host firewall (nftables) to")
        out.append("# restrict by CIDR if needed.")

    return out


def _decrypt_or_none(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return decrypt_str(blob)
    except Exception:  # noqa: BLE001 — bad ciphertext = treat as unset
        return None


def _decrypt_v3_pass(token: Any) -> str | None:
    """v3 passes are stored as the URL-safe-base64 string Fernet emits,
    so ``decrypt_str`` expects bytes — encode back before decrypting.
    Returns None if the token is missing or undecryptable."""
    if not token or not isinstance(token, str):
        return None
    try:
        return decrypt_str(token.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None


def snmp_bundle(settings: PlatformSettings) -> dict[str, Any]:
    """Build the ``snmp_settings`` block that the DNS + DHCP
    ConfigBundle ships to agents.

    Includes the rendered ``snmpd.conf`` body and a config-hash the
    agent can short-circuit on (avoid writing the trigger file when
    the body hasn't changed since the last bundle).
    """
    body = render_snmpd_conf(settings) if settings.snmp_enabled else ""
    config_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else ""
    return {
        "enabled": bool(settings.snmp_enabled),
        "config_hash": config_hash,
        "snmpd_conf": body,
    }

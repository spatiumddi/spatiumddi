"""Issue #156 — appliance syslog (rsyslog) forwarding.

Source-of-truth lives on ``platform_settings`` (singleton); this module
renders an ``/etc/rsyslog.d/50-spatium-forward.conf`` payload from those
columns and folds the rendered text into the DNS + DHCP ConfigBundle long-
poll + the supervisor heartbeat so every appliance host (local + remote
agents) picks it up the same way Issue #153 (SNMP) / #154 (NTP) / #343
(LLDP) distribute their host config.

Design notes:

* rsyslog runs at the Debian host level on the appliance image, NOT in a
  container — it needs to read journald (``imjournal``) + host file sources
  and reach the network egress directly. The trigger-file → host systemd
  path-unit pattern lets the api / supervisor ship config without itself
  being on the host.
* Forwarding is OUTBOUND only (one ``omfwd`` action per target), so unlike
  SNMP (UDP 161) / NTP-serve (UDP 123) there is NO nftables drop-in — the
  appliance opens nothing inbound.
* journald is forwarded explicitly via an ``imjournal`` input block so the
  systemd journal's logs actually ship, not just file sources. Debian's
  stock rsyslog also wires ``imuxsock`` for the legacy ``/dev/log`` socket;
  ``imjournal`` is the authoritative path on a journald system.
* TLS targets use the ``gtls`` driver. Each TLS target's operator-supplied
  CA PEM is written to its own ``.pem`` file by the host runner (filename is
  deterministic per target index) and referenced by a per-action
  ``StreamDriverCAFile``. The CA PEM is stored Fernet-encrypted at rest
  (URL-safe-base64 string the way the SNMP v3 passes are) and decrypted here
  via :func:`app.core.crypto.decrypt_str`.
* ``syslog_bundle`` returns a STABLE dict shape even when disabled
  (``{enabled, config_hash, rsyslog_conf, ca_certs}``) so the agent /
  supervisor hash-compare never KeyErrors — same contract the SNMP / NTP /
  LLDP bundles follow.
* Empty / disabled settings still produce a deterministic body so the
  host-side runner can compare byte-for-byte against the on-disk file and
  skip the reload when nothing changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.crypto import decrypt_str
from app.models.settings import PlatformSettings

# Strict host charset shared by BOTH write paths (REST PUT +
# AI proposal) and re-checked defensively at render time. The host is
# interpolated into a quoted RainerScript ``target="..."`` action param,
# so anything outside a hostname / IP-literal character class (quotes,
# backslashes, whitespace, control chars) could break out of the string
# and inject action params into the root-owned rsyslog config. Allow
# only the characters that appear in DNS hostnames + IPv4/IPv6 literals:
# letters, digits, dot, colon (IPv6), hyphen, underscore. No ``[]``
# brackets — rsyslog's omfwd ``target`` takes a bare IPv6 literal.
_SYSLOG_HOST_RE = re.compile(r"\A[A-Za-z0-9.:_-]+\Z")

# rsyslog selector charset — ``facility.severity`` tokens, comma-joined,
# plus ``*`` wildcards and the ``!`` / ``=`` / ``;`` / ``:`` modifiers.
# Rendered straight into the conf body, so reject control chars /
# newlines / quotes to keep config injection out. Shared by the REST PUT
# validator + the AI proposal path.
_SYSLOG_FILTER_RE = re.compile(r"[A-Za-z0-9_*.,;:=!\- ]+")


def validate_syslog_host(host: str) -> str:
    """Validate + normalise a syslog forward-target host.

    Returns the stripped host when valid; raises ``ValueError`` with an
    operator-facing message otherwise. Shared by the REST PUT validator
    (``SyslogTargetUpdate``) and the AI proposal validator
    (``SyslogTargetArg`` / ``_validate_syslog_targets``) so both write
    paths enforce identical rules — the host lands in a quoted
    RainerScript action param in the root-owned rsyslog config, so a
    value with a double-quote / backslash / whitespace / control char
    must never reach the renderer.
    """
    s = (host or "").strip()
    if not s:
        raise ValueError("host may not be empty")
    if not _SYSLOG_HOST_RE.fullmatch(s):
        raise ValueError(
            "host may only contain letters, digits, and . : - _ "
            "(hostname or IP literal); no quotes, backslashes, "
            "whitespace, or control characters"
        )
    return s


def validate_syslog_filter(value: str | None) -> str | None:
    """Validate + normalise an rsyslog selector string.

    ``None`` passes through unchanged (means "leave as-is" on the REST
    PUT). A stripped non-empty value must match the selector charset or
    ``ValueError`` is raised. Shared by the REST PUT
    (``_valid_syslog_filter``) + the AI proposal path so a filter with a
    newline / directive char can't inject into the root-owned conf via
    either write surface.
    """
    if value is None:
        return None
    v = value.strip()
    if v and not _SYSLOG_FILTER_RE.fullmatch(v):
        raise ValueError(
            "syslog_filter may only contain letters, digits, and * . , ; : = ! - _ space"
        )
    return v


# rsyslog severity-mapping for the RFC5424 / RFC3164 framing. The
# operator picks the wire format per-target; we translate to the
# rsyslog template + protocol-driver settings the ``omfwd`` action
# needs. These template names are emitted once at the top of the
# rendered file and referenced by each action.
_TEMPLATES = {
    # RFC 5424 structured-data format (modern syslog). rsyslog ships a
    # built-in ``RSYSLOG_SyslogProtocol23Format`` template that emits
    # exactly this; we reference it directly rather than re-declaring.
    "rfc5424": "RSYSLOG_SyslogProtocol23Format",
    # RFC 3164 legacy BSD format — rsyslog's built-in
    # ``RSYSLOG_TraditionalForwardFormat``.
    "rfc3164": "RSYSLOG_TraditionalForwardFormat",
    # JSON-per-message. Declared inline below as ``SpatiumJSON`` since
    # rsyslog has no built-in all-fields JSON template.
    "json": "SpatiumJSON",
}

# Per-target CA filename convention shared with the host runner. The
# runner writes each TLS target's decrypted PEM to this path (index is
# the target's position in ``syslog_targets``) and the rendered action
# references it via ``StreamDriverCAFile``.
_CA_DIR = "/etc/rsyslog.d/spatium-ca"


def _ca_filename(index: int) -> str:
    """Deterministic per-target CA PEM filename (host-side path)."""
    return f"{_CA_DIR}/target-{index}.pem"


def _decrypt_or_none(token: Any) -> str | None:
    """CA PEMs are stored as the URL-safe-base64 string Fernet emits,
    so ``decrypt_str`` expects bytes — encode back before decrypting.
    Returns None if the token is missing or undecryptable (treated as
    "no CA configured", which the renderer notes inline)."""
    if not token or not isinstance(token, str):
        return None
    try:
        return decrypt_str(token.encode("ascii"))
    except Exception:  # noqa: BLE001 — bad ciphertext = treat as unset
        return None


def render_rsyslog_conf(settings: PlatformSettings) -> str:
    """Return the full ``/etc/rsyslog.d/50-spatium-forward.conf`` body.

    Deterministic — same settings → same bytes — so the agent's /
    supervisor's config-hash idempotency check stays stable across
    long-poll cycles. The host-side runner re-reads the on-disk hash
    sidecar and skips the reload when the bundle hash matches.
    """
    lines: list[str] = [
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.",
        "# Source of truth: /appliance → Syslog in the SpatiumDDI UI.",
        "",
    ]

    # journald ingestion. ``imjournal`` reads the systemd journal
    # directly so journald-routed logs ship, not just /dev/log file
    # sources. ``StateFile`` lets rsyslog resume where it left off
    # across restarts so a reload doesn't re-ship the whole journal.
    lines.extend(
        [
            "# Forward the systemd journal — without imjournal only file / socket",
            "# sources would ship, and on a journald system that's almost nothing.",
            'module(load="imjournal" StateFile="spatium-imjournal.state")',
            "",
            "# JSON forward template (one JSON object per message) — referenced by",
            "# targets configured with format=json.",
            'template(name="SpatiumJSON" type="list" option.jsonf="on") {',
            '    property(outname="timestamp" name="timereported" '
            'dateFormat="rfc3339" format="jsonf")',
            '    property(outname="host" name="hostname" format="jsonf")',
            '    property(outname="severity" name="syslogseverity-text" format="jsonf")',
            '    property(outname="facility" name="syslogfacility-text" format="jsonf")',
            '    property(outname="tag" name="syslogtag" format="jsonf")',
            '    property(outname="message" name="msg" format="jsonf")',
            "}",
            "",
        ]
    )

    targets = list(settings.syslog_targets or [])
    selector = (settings.syslog_filter or "").strip() or "*.*"
    buffer_disk = bool(settings.syslog_buffer_disk)

    if not targets:
        lines.append(
            "# No forward targets configured — rsyslog runs but ships nothing. "
            "Add a destination in the UI."
        )
        lines.append("")
        return "\n".join(lines) + "\n"

    for index, raw in enumerate(targets):
        if not isinstance(raw, dict):
            continue
        host = (raw.get("host") or "").strip()
        if not host:
            continue
        # Belt-and-suspenders: both write paths validate the host, but
        # never trust a stored value — a host that fails the strict
        # charset (e.g. an embedded quote / backslash from a malformed
        # JSONB row) would break out of the quoted ``target="..."`` param
        # and inject action params into this root-owned config, so drop it.
        if not _SYSLOG_HOST_RE.fullmatch(host):
            lines.append(
                f"# Target {index}: dropped — host {host!r} contains "
                "characters invalid for an rsyslog forward target."
            )
            lines.append("")
            continue
        port = int(raw.get("port") or 514)
        protocol = (raw.get("protocol") or "udp").strip().lower()
        fmt = (raw.get("format") or "rfc5424").strip().lower()
        template = _TEMPLATES.get(fmt, _TEMPLATES["rfc5424"])

        lines.append(f"# Target {index}: {host}:{port} ({protocol}, {fmt})")
        # The selector goes on the action line itself (rsyslog's
        # ``<selector> action(...)`` form scopes the action to matching
        # messages). Build the omfwd action params.
        action_params: list[str] = [
            'type="omfwd"',
            f'target="{host}"',
            f'port="{port}"',
            f'template="{template}"',
        ]
        if protocol == "tls":
            # gtls stream driver — the CA PEM is written per-target by
            # the host runner; reference it here. ``StreamDriverMode=1``
            # = TLS-encrypted; ``StreamDriverAuthMode=x509/name`` checks
            # the cert against the target name.
            ca_pem = _decrypt_or_none(raw.get("ca_cert_pem"))
            action_params.append('protocol="tcp"')
            action_params.append('StreamDriver="gtls"')
            action_params.append('StreamDriverMode="1"')
            action_params.append('StreamDriverAuthMode="x509/name"')
            if ca_pem:
                action_params.append(f'StreamDriverCAFile="{_ca_filename(index)}"')
            else:
                lines.append(
                    "#   TLS selected but no CA PEM stored — connection will "
                    "fail cert validation until one is provided."
                )
        elif protocol == "tcp":
            action_params.append('protocol="tcp"')
        else:
            action_params.append('protocol="udp"')

        if buffer_disk:
            # Disk-assisted queue so a brief collector outage doesn't
            # drop logs. Each action gets its own spool prefix so
            # multiple targets don't collide on disk.
            action_params.append('queue.type="LinkedList"')
            action_params.append(f'queue.filename="spatium-fwd-{index}"')
            action_params.append('queue.maxdiskspace="256m"')
            action_params.append('queue.saveonshutdown="on"')
            action_params.append('action.resumeRetryCount="-1"')

        params_block = "\n    ".join(action_params)
        lines.append(f"{selector} action(\n    {params_block}\n)")
        lines.append("")

    return "\n".join(lines) + "\n"


def syslog_bundle(settings: PlatformSettings) -> dict[str, Any]:
    """Build the ``syslog_settings`` block shipped to agents + supervisor.

    Returns a STABLE dict shape even when disabled so every hash-compare
    caller (DHCP-agent ConfigBundle ETag mix, supervisor heartbeat
    ``maybe_fire_syslog_reload``) reads the same keys without a KeyError:

      * ``enabled`` — the master toggle.
      * ``config_hash`` — sha256 of the rendered body PLUS a
        deterministic digest of ``ca_certs`` (empty string when
        disabled, so an enabled→disabled flip still shifts the hash).
        The CA material is folded in because a TLS target references its
        CA by a deterministic *path* (``StreamDriverCAFile``) that never
        changes when the PEM is rotated — so a same-host/port/protocol/
        format CA swap leaves the rendered body byte-identical. Without
        hashing the PEM bytes the supervisor's ``maybe_fire`` trigger
        would never fire and the appliance would keep validating against
        the OLD CA forever (#156 review).
      * ``rsyslog_conf`` — the rendered body, written verbatim by the
        host runner (empty when disabled).
      * ``ca_certs`` — ``{filename: pem}`` of every TLS target's
        decrypted CA PEM, so the host runner can stage them alongside
        the conf. Empty dict when disabled or no TLS targets.
    """
    enabled = bool(settings.syslog_enabled)
    body = render_rsyslog_conf(settings) if enabled else ""
    ca_certs: dict[str, str] = {}
    if enabled:
        for index, raw in enumerate(list(settings.syslog_targets or [])):
            if not isinstance(raw, dict):
                continue
            if (raw.get("protocol") or "").strip().lower() != "tls":
                continue
            pem = _decrypt_or_none(raw.get("ca_cert_pem"))
            if pem:
                ca_certs[_ca_filename(index)] = pem
    if body:
        # Fold the CA material into the hash so rotating a CA (same
        # host/port/protocol/format, new PEM) shifts config_hash and the
        # supervisor trigger fires. ``sort_keys=True`` keeps the digest
        # deterministic regardless of dict insertion order.
        ca_digest = json.dumps(ca_certs, sort_keys=True)
        config_hash = hashlib.sha256((body + "\n" + ca_digest).encode("utf-8")).hexdigest()
    else:
        config_hash = ""
    return {
        "enabled": enabled,
        "config_hash": config_hash,
        "rsyslog_conf": body,
        "ca_certs": ca_certs,
    }

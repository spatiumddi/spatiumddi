"""Issue #158 — appliance DNS resolver (systemd-resolved).

Source-of-truth lives on ``platform_settings`` (singleton); this module
renders a ``/etc/systemd/resolved.conf.d/spatiumddi.conf`` ``[Resolve]``
drop-in from those columns and folds the rendered text into the DNS + DHCP
ConfigBundle long-poll + the supervisor heartbeat so every appliance host
(local + remote agents) picks it up the same way #153 (SNMP) / #154 (NTP) /
#343 (LLDP) / #156 (syslog) / #157 (SSH) distribute their host config.

Design notes:

* systemd-resolved runs at the Debian host level on the appliance image,
  NOT in a container. The trigger-file → host systemd path-unit pattern lets
  the api / supervisor ship config without itself being on the host.
* ``resolver_mode='automatic'`` (the default) leaves systemd-resolved to
  pick upstream DNS from per-link NetworkManager / DHCP. In that state the
  bundle is "disabled" — an empty config_hash — and the host runner REMOVES
  the spatiumddi.conf drop-in (leaving the image-shipped
  ``no-stub-listener.conf`` intact). ``resolver_mode='override'`` pins a
  global server list.
* The drop-in NEVER emits ``DNSStubListener`` — the image-shipped
  ``/etc/systemd/resolved.conf.d/no-stub-listener.conf`` owns that knob
  (BIND9 binds host :53, which overlaps the 127.0.0.53 stub listener, so the
  stub must stay off). The runner removes ONLY spatiumddi.conf on a revert,
  never no-stub-listener.conf.
* In override mode the renderer ALSO emits ``Domains=~.`` (a route-only
  default domain) AHEAD of any configured search domains. Without it,
  systemd-resolved keeps routing queries to the per-link
  NetworkManager/DHCP-provided resolvers for names that match their search
  domains, so the global ``DNS=`` servers wouldn't actually win. ``~.``
  routes the catch-all to the global servers so the override has teeth.
* Resolver IPs / domains are NOT secrets — the read shape mirrors the stored
  shape directly (no Fernet, no redaction), like NTP server hostnames / SSH
  public keys.
* ``resolver_bundle`` returns a STABLE dict shape even when "disabled" (the
  default automatic state) so every hash-compare caller (DHCP-agent ETag
  mix, supervisor heartbeat ``maybe_fire_resolver_reload``) reads the same
  keys without a KeyError — same contract the SNMP / NTP / LLDP / syslog /
  SSH bundles follow.
* Empty / disabled settings still produce a deterministic body so the
  host-side runner can compare byte-for-byte against the on-disk file and
  skip the reload when nothing changed.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.models.settings import PlatformSettings


def render_resolved_conf(settings: PlatformSettings) -> str:
    """Return the full ``/etc/systemd/resolved.conf.d/spatiumddi.conf``
    ``[Resolve]`` drop-in body for these settings (override mode).

    Deterministic — same inputs yield same bytes — so the agent's
    config-hash idempotency check stays stable across long-poll cycles. The
    host runner re-reads the on-disk hash sidecar and skips the reload when
    the bundle hash matches.

    NEVER emits ``DNSStubListener`` — the image-shipped no-stub-listener.conf
    owns that knob (BIND9 binds host :53). Emits ``Domains=~.`` ahead of any
    configured search domains so the global ``DNS=`` servers win over the
    per-link NetworkManager/DHCP resolvers.
    """
    servers = [s.strip() for s in (settings.resolver_servers or []) if str(s).strip()]
    fallback = [s.strip() for s in (settings.resolver_fallback_servers or []) if str(s).strip()]
    search = [d.strip() for d in (settings.resolver_search_domains or []) if str(d).strip()]
    dnssec = (settings.resolver_dnssec or "allow-downgrade").strip()
    dot = (settings.resolver_dns_over_tls or "no").strip()

    lines: list[str] = [
        "# Managed by SpatiumDDI — edits will be overwritten on next config push.",
        "# Source of truth: /appliance → DNS Resolver in the SpatiumDDI UI.",
        "#",
        "# The stub-listener knob is intentionally NOT set here — the",
        "# image-shipped no-stub-listener.conf owns it (BIND9 binds host :53).",
        "# This drop-in only steers upstream resolver selection.",
        "[Resolve]",
    ]

    # ``DNS=`` / ``FallbackDNS=`` take a space-separated server list on a
    # single line — systemd's documented form.
    if servers:
        lines.append("DNS=" + " ".join(servers))
    else:
        # Override mode with no servers configured is still a valid body —
        # an empty DNS= line is harmless; document it so an operator who
        # SSHes in understands the half-configured state.
        lines.append("# No upstream DNS servers configured — add some in the UI.")

    if fallback:
        lines.append("FallbackDNS=" + " ".join(fallback))

    # Route-only default domain FIRST so the global DNS= servers win over the
    # per-link NetworkManager/DHCP resolvers, then any operator search domains.
    domains = ["~."] + search
    lines.append("Domains=" + " ".join(domains))

    lines.append(f"DNSSEC={dnssec}")
    lines.append(f"DNSOverTLS={dot}")

    # Trailing newline so editors don't add a "no newline at end of file"
    # diff if an operator SSHes in and inspects the file.
    return "\n".join(lines) + "\n"


def resolver_bundle(settings: PlatformSettings) -> dict[str, Any]:
    """Build the ``resolver_settings`` block shipped to agents + supervisor.

    Returns a STABLE dict shape so every hash-compare caller (DHCP-agent
    ConfigBundle ETag mix, supervisor heartbeat ``maybe_fire_resolver_reload``)
    reads the same keys without a KeyError:

      * ``enabled`` — True only in ``override`` mode. In ``automatic`` mode
        the host runner removes the spatiumddi.conf drop-in (leaving
        no-stub-listener.conf intact) so resolved falls back to per-link
        DHCP/NetworkManager DNS.
      * ``config_hash`` — sha256 of the rendered drop-in body in override
        mode; empty string when disabled (automatic) so the agent's on-disk
        sidecar compare short-circuits.
      * ``resolved_conf`` — the rendered drop-in body; only meaningful when
        enabled (empty string when automatic).
    """
    enabled = (settings.resolver_mode or "automatic").strip() == "override"
    body = render_resolved_conf(settings) if enabled else ""
    config_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() if enabled else ""
    return {
        "enabled": enabled,
        "config_hash": config_hash,
        "resolved_conf": body,
    }

"""BIND9 agent driver.

Renders ``named.conf`` and zone files under ``/var/lib/spatium-dns-agent/rendered``,
validates with ``named-checkconf``, atomically swaps, and reloads via ``rndc``.
Record ops are applied via ``nsupdate`` over loopback, authenticated with the
TSIG key carried in the config bundle.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import structlog

try:
    import dns.query
    import dns.tsigkeyring
    import dns.update
except ImportError:  # pragma: no cover - runtime-optional
    dns = None  # type: ignore[assignment]

from .base import DriverBase

log = structlog.get_logger(__name__)

NAMED_CONF_SKELETON = """\
options {{
    directory "/var/cache/bind";
    listen-on {{ any; }};
    listen-on-v6 {{ any; }};
    recursion {recursion};
    allow-query {{ {allow_query}; }};
    dnssec-validation {dnssec};
    check-integrity no;
{forwarders}{response_policy}
}};
statistics-channels {{
    inet 127.0.0.1 port 8053 allow {{ 127.0.0.1; }};
}};
{tsig_include}"""


class Bind9Driver(DriverBase):
    rendered_dir_name = "rendered"
    daemon_pid: int | None = None

    # ── Render / validate / swap ────────────────────────────────────────────

    def render(self, bundle: dict[str, Any]) -> None:
        new_dir = self.state_dir / "rendered.new"
        if new_dir.exists():
            shutil.rmtree(new_dir)
        (new_dir / "zones").mkdir(parents=True)

        opts = bundle.get("options", {})
        forwarders = opts.get("forwarders") or []
        recursion = "yes" if opts.get("recursion_enabled", True) else "no"
        allow_query = "; ".join(opts.get("allow_query") or ["any"])
        dnssec = opts.get("dnssec_validation", "auto")
        # RPZ rewrites break DNSSEC chain validation: even with
        # `break-dnssec yes`, BIND9 returns SERVFAIL to clients that set the
        # DO bit on DNSSEC-signed domains being blocked. For a blocking
        # appliance the user intent is "block, don't validate", so silently
        # disable validation when blocklists are present.
        if bundle.get("blocklists"):
            dnssec = "no"
        fwd_block = ""
        if forwarders:
            fwd_block = "    forwarders {{ {fs}; }};\n".format(
                fs="; ".join(forwarders)
            )

        tsig_keys = bundle.get("tsig_keys") or []
        tsig_key_name = tsig_keys[0]["name"] if tsig_keys else None
        tsig_include = (
            'include "/var/lib/spatium-dns-agent/tsig/ddns.key";\n' if tsig_keys else ""
        )

        # Response-policy block needs to list every RPZ zone we're about to
        # declare, otherwise BIND9 won't consult them on lookups.
        blocklists = bundle.get("blocklists") or []
        response_policy_block = ""
        if blocklists:
            zones_list = "; ".join(
                f'zone "{bl["rpz_zone_name"].rstrip(".")}"' for bl in blocklists
            )
            # break-dnssec lets RPZ rewrite responses from DNSSEC-signed zones
            # (otherwise BIND9 returns SERVFAIL on a DNSSEC conflict). For a
            # blocking use-case this is what you want: the user intent is to
            # block, not to preserve validation integrity.
            response_policy_block = (
                f"    response-policy {{ {zones_list}; }} break-dnssec yes;\n"
            )

        conf = NAMED_CONF_SKELETON.format(
            recursion=recursion,
            allow_query=allow_query,
            dnssec=dnssec,
            forwarders=fwd_block,
            response_policy=response_policy_block,
            tsig_include=tsig_include,
        )

        for zone in bundle.get("zones", []):
            zname = zone.get("name") or ""
            if not zname:
                continue
            # Relative path used inside the rendered tree; absolute path
            # written into named.conf so BIND9 doesn't resolve against its
            # `directory` (which is /var/cache/bind, not our rendered tree).
            rel_zfile = f"zones/{zname.rstrip('.')}.db"
            current_dir = self.state_dir / self.rendered_dir_name
            abs_zfile = current_dir / rel_zfile
            zone_type = zone.get("type", "primary")
            bind_type = "master" if zone_type in {"primary", "master"} else "slave"
            allow_update = (
                f'allow-update {{ key "{tsig_key_name}"; }}; ' if tsig_key_name else ""
            )
            conf += (
                f'zone "{zname}" {{ type {bind_type}; file "{abs_zfile}"; '
                f'{allow_update}}};\n'
            )
            if zone_type in {"primary", "master"}:
                self._write_zone_file(new_dir / rel_zfile, zone)

        # RPZ zones for blocklists. Each blocklist becomes a master zone named
        # after its rpz_zone_name. Entries are rendered as CNAME records:
        # - nxdomain     → CNAME .                  (synthesize NXDOMAIN)
        # - sinkhole     → CNAME rpz-drop.          (drop silently)
        # - redirect     → CNAME <target>.          (CNAME to target)
        # Exceptions (allow-list) are CNAME rpz-passthru.  (explicit bypass)
        for bl in blocklists:
            zname = bl["rpz_zone_name"]
            rel = f"zones/{zname.rstrip('.')}.db"
            abs_zfile = self.state_dir / self.rendered_dir_name / rel
            conf += (
                f'zone "{zname}" {{ type master; file "{abs_zfile}"; '
                f"allow-query {{ localhost; }}; }};\n"
            )
            self._write_rpz_zone_file(new_dir / rel, bl)

        (new_dir / "named.conf").write_text(conf)

        # TSIG key — written to tsig/ddns.key (stable path)
        if tsig_keys:
            tsig_dir = self.state_dir / "tsig"
            tsig_dir.mkdir(parents=True, exist_ok=True)
            k = tsig_keys[0]
            tsig_file = tsig_dir / "ddns.key"
            tsig_file.write_text(
                f'key "{k["name"]}" {{ algorithm {k.get("algorithm", "hmac-sha256")}; '
                f'secret "{k["secret"]}"; }};\n'
            )
            os.chmod(tsig_file, 0o600)

    def _write_zone_file(self, path: Path, zone: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        name = zone.get("name") or ""
        ttl = zone.get("ttl", 3600)
        serial = zone.get("serial") or 1
        # Auto-emit a self-referential glue A record so BIND9 accepts the
        # zone even when the user didn't explicitly add `ns1 IN A …`.
        # 127.0.0.1 is fine for dev; production should set primary_ns + glue
        # explicitly via the zone create form.
        lines = [
            f"$TTL {ttl}",
            f"@ IN SOA ns1.{name} admin.{name} ( {serial} 3600 600 86400 300 )",
            f"@ IN NS ns1.{name}",
            f"ns1 IN A 127.0.0.1",
        ]
        for rec in zone.get("records", []) or []:
            rec_ttl = rec.get("ttl") or ttl
            name_field = rec.get("name") or "@"
            rtype = rec["type"].upper()
            value = rec["value"]
            # MX / SRV zone-file format requires inline priority (and
            # weight+port for SRV) before the target. The control plane
            # stores those in separate columns; compose the wire shape
            # here so ``named-checkzone`` parses the zone cleanly.
            if rtype == "MX" and rec.get("priority") is not None:
                if not value.lstrip().split(" ", 1)[0].isdigit():
                    value = f"{rec['priority']} {value}"
            elif (
                rtype == "SRV"
                and rec.get("priority") is not None
                and rec.get("weight") is not None
                and rec.get("port") is not None
                and len(value.split()) < 4
            ):
                value = f"{rec['priority']} {rec['weight']} {rec['port']} {value}"
            lines.append(f"{name_field} {rec_ttl} IN {rtype} {value}")
        path.write_text("\n".join(lines) + "\n")

    def _write_rpz_zone_file(self, path: Path, bl: dict[str, Any]) -> None:
        """Render an RPZ zone file.

        RPZ uses CNAME trigger records to tell BIND9 how to rewrite responses:
          - CNAME .            → synthesize NXDOMAIN
          - CNAME *.           → synthesize NODATA
          - CNAME rpz-drop.    → drop the query (no response)
          - CNAME rpz-passthru → explicit bypass (used for exceptions)
          - CNAME <target>.    → rewrite response to CNAME target

        Wildcard entries are emitted as `*.<domain>` to catch subdomains.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        zname = bl["rpz_zone_name"]
        lines = [
            "$TTL 60",
            f"@ IN SOA localhost. root.localhost. ( 1 3600 600 86400 60 )",
            "@ IN NS localhost.",
        ]
        for e in bl.get("entries") or []:
            domain = e["domain"].rstrip(".")
            action = e.get("action") or "block"
            block_mode = e.get("block_mode") or "nxdomain"
            is_wildcard = bool(e.get("is_wildcard"))
            target = e.get("target")
            if action == "redirect" and target:
                rhs = f"{target.rstrip('.')}."
            elif block_mode == "sinkhole":
                rhs = "rpz-drop."
            else:  # default: nxdomain
                rhs = "."
            lines.append(f"{domain} CNAME {rhs}")
            if is_wildcard:
                lines.append(f"*.{domain} CNAME {rhs}")
        # Exceptions → passthrough (never blocked even if a broader rule matches).
        for exc in bl.get("exceptions") or []:
            d = exc.rstrip(".")
            lines.append(f"{d} CNAME rpz-passthru.")
            lines.append(f"*.{d} CNAME rpz-passthru.")
        path.write_text("\n".join(lines) + "\n")
        log.info("bind9_rpz_written", zone=zname, entries=len(bl.get("entries") or []))

    def validate(self) -> None:
        new_dir = self.state_dir / "rendered.new"
        conf = new_dir / "named.conf"
        if not shutil.which("named-checkconf"):
            log.warning("named_checkconf_missing_skipping")
            return
        res = subprocess.run(
            ["named-checkconf", str(conf)], capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            raise RuntimeError(f"named-checkconf failed: {res.stderr.strip()}")

    def swap_and_reload(self) -> None:
        new_dir = self.state_dir / "rendered.new"
        current = self.state_dir / self.rendered_dir_name
        backup = self.state_dir / "rendered.prev"
        if current.exists():
            if backup.exists():
                shutil.rmtree(backup)
            current.rename(backup)
        new_dir.rename(current)
        # Signal daemon. Try rndc first; if it isn't configured (no rndc.key),
        # fall back to SIGHUP which named handles as a config + zone reload.
        rndc_ok = False
        if shutil.which("rndc"):
            res = subprocess.run(
                ["rndc", "reconfig"], capture_output=True, text=True, check=False
            )
            rndc_ok = res.returncode == 0
            if not rndc_ok:
                log.warning("rndc_failed_falling_back_to_sighup", stderr=res.stderr.strip())
        if not rndc_ok and self.daemon_pid:
            try:
                os.kill(self.daemon_pid, signal.SIGHUP)
                log.info("named_sighup_sent", pid=self.daemon_pid)
            except OSError as e:
                log.error("named_sighup_failed", error=str(e))

    # ── Record ops (RFC 2136 over loopback) ─────────────────────────────────

    def apply_record_op(self, op: dict[str, Any]) -> None:
        if dns is None:
            raise RuntimeError("dnspython not installed — cannot apply record ops")
        zone = op["zone_name"].rstrip(".") + "."
        rec = op["record"]
        name = rec.get("name") or "@"
        rtype = rec["type"]
        value = rec["value"]
        # rec.get returns None when the field exists with null value (which
        # is the common case from JSON), so fall back explicitly.
        ttl_value = rec.get("ttl")
        ttl = ttl_value if ttl_value is not None else 3600

        tsig_path = self.state_dir / "tsig" / "ddns.key"
        keyring = None
        if tsig_path.exists():
            # Very small parser — supports the one-line key we render above.
            content = tsig_path.read_text()
            import re
            m = re.search(r'key\s+"([^"]+)".*?secret\s+"([^"]+)"', content, re.DOTALL)
            if m:
                keyring = dns.tsigkeyring.from_text({m.group(1): m.group(2)})

        # MX / SRV wire-format requires the priority (and weight+port) to
        # appear inline before the target. The control plane stores those
        # as separate columns and, historically, only forwarded `value`.
        # Prefer explicit fields; fall back to the raw value if an already-
        # composed wire string came through (legacy path + future-proofing).
        wire_value = value
        rtype_u = rtype.upper()
        if rtype_u == "MX":
            pri = rec.get("priority")
            if pri is not None and not value.lstrip().split(" ", 1)[0].isdigit():
                wire_value = f"{pri} {value}"
        elif rtype_u == "SRV":
            pri = rec.get("priority")
            wt = rec.get("weight")
            prt = rec.get("port")
            if (
                pri is not None
                and wt is not None
                and prt is not None
                and len(value.split()) < 4
            ):
                wire_value = f"{pri} {wt} {prt} {value}"

        upd = dns.update.Update(zone, keyring=keyring)
        if op["op"] in ("create", "update"):
            upd.replace(name, ttl, rtype, wire_value)
        elif op["op"] == "delete":
            # Some BIND configurations reject the RR-specific delete form
            # (value must exactly match a live RR) when the running daemon
            # has drifted from the zone file. Delete by (name, rtype) so any
            # matching RR gets cleared. Idempotent.
            upd.delete(name, rtype)
        else:
            raise ValueError(f"unknown op: {op['op']}")
        resp = dns.query.tcp(upd, "127.0.0.1", timeout=10)
        rcode = resp.rcode()
        if rcode != 0:  # NOERROR
            raise RuntimeError(
                f"nsupdate returned rcode={rcode} "
                f"(zone={zone} op={op['op']} name={name} type={rtype})"
            )

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start_daemon(self) -> None:
        conf_path = self.state_dir / self.rendered_dir_name / "named.conf"
        if not conf_path.exists():
            log.warning("named_conf_missing_startup_deferred")
            return
        # -g: foreground, log to stderr. We're already running unprivileged
        # as 'spatium' (entrypoint dropped privs via su-exec), so don't pass
        # -u — named would try to setgid() to a different user and fail.
        self.daemon_pid = subprocess.Popen(
            ["named", "-g", "-c", str(conf_path)]
        ).pid
        log.info("named_started", pid=self.daemon_pid)

    def daemon_running(self) -> bool:
        if self.daemon_pid is None:
            return False
        try:
            os.kill(self.daemon_pid, 0)
            return True
        except OSError:
            return False

"""BIND9 agent driver.

Renders ``named.conf`` and zone files under ``/var/lib/spatium-dns-agent/rendered``,
validates with ``named-checkconf``, atomically swaps, and reloads via ``rndc``.
Record ops are applied via ``nsupdate`` over loopback, authenticated with the
TSIG key carried in the config bundle.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import time
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
{logging_block}{tsig_include}"""

# Query-log channel + category block. Path matches the QueryLogShipper
# default (``/var/log/named/queries.log``) so the shipper can tail the
# same file the daemon writes — the entrypoint script chowns this
# directory to the unprivileged ``spatium`` user at boot. Severity +
# print-* defaults match ``DNSServerOptions``' column defaults; we
# don't plumb the per-field overrides through the agent yet because
# the operator-facing UI surfaces the boolean toggle only.
_QUERY_LOG_BLOCK = """\
logging {
    channel queries_channel {
        file "/var/log/named/queries.log" versions 5 size 50m;
        severity info;
        print-category yes;
        print-severity yes;
        print-time yes;
    };
    category queries { queries_channel; };
    category query-errors { queries_channel; };
};
"""


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
            fwd_block = "    forwarders {{ {fs}; }};\n".format(fs="; ".join(forwarders))

        tsig_keys = bundle.get("tsig_keys") or []
        tsig_key_name = tsig_keys[0]["name"] if tsig_keys else None
        tsig_include = (
            'include "/var/lib/spatium-dns-agent/tsig/ddns.key";\n' if tsig_keys else ""
        )

        # Split-horizon (issue #24): when the group defines views, every
        # zone — and every RPZ/response-policy — lives INSIDE a
        # ``view { match-clients … }`` block. BIND9 forbids mixing
        # top-level zones with views, so the global response-policy below
        # is only emitted in the no-views path; with views it's rendered
        # per-view further down.
        views = bundle.get("views") or []
        has_views = bool(views)

        # Response-policy block needs to list every RPZ zone we're about to
        # declare, otherwise BIND9 won't consult them on lookups.
        blocklists = bundle.get("blocklists") or []
        response_policy_block = ""
        if blocklists and not has_views:
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

        logging_block = _QUERY_LOG_BLOCK if bool(opts.get("query_log_enabled")) else ""
        conf = NAMED_CONF_SKELETON.format(
            recursion=recursion,
            allow_query=allow_query,
            dnssec=dnssec,
            forwarders=fwd_block,
            response_policy=response_policy_block,
            logging_block=logging_block,
            tsig_include=tsig_include,
        )

        # Explicit controls block keyed off the agent-generated rndc.key
        # (written by the entrypoint at first boot). Without this, BIND9
        # auto-generates an in-memory rndc key that doesn't match the
        # on-disk file, so `rndc reconfig` + the rndc-status pusher both
        # fail with "bad auth". The same key lives on disk under
        # state_dir/rndc.key with an `rndc.conf` wrapper the agent uses
        # for every CLI invocation.
        rndc_key_path = self.state_dir / "rndc.key"
        if rndc_key_path.exists():
            conf += (
                f'include "{rndc_key_path}";\n'
                "controls {\n"
                "    inet 127.0.0.1 port 953 allow { 127.0.0.1; } "
                'keys { "spatium-rndc"; };\n'
                "};\n"
            )

        def _zone_stanza(zone: dict[str, Any], file_prefix: str) -> str:
            """Build one ``zone "..." { ... };`` and write its zone file.

            ``file_prefix`` namespaces the on-disk file (e.g.
            ``"internal/"``) so the SAME zone name served from multiple
            views doesn't clobber files (issue #24). Returns "" for a
            zone that shouldn't be emitted (forward zone w/o upstreams).
            """
            zname = zone.get("name") or ""
            if not zname:
                return ""
            zone_type = zone.get("type", "primary")
            # Forward zones: just a forwarders block, no file / allow-update.
            if zone_type == "forward":
                fwds = [str(f) for f in (zone.get("forwarders") or []) if f]
                if not fwds:
                    return ""
                policy = "only" if bool(zone.get("forward_only", True)) else "first"
                return (
                    f'zone "{zname}" {{ type forward; forward {policy}; '
                    f'forwarders {{ {"; ".join(fwds)}; }}; }};\n'
                )
            # Relative path inside the rendered tree; absolute path written
            # into named.conf so BIND9 doesn't resolve against its
            # `directory` (/var/cache/bind, not our rendered tree).
            rel_zfile = f"zones/{file_prefix}{zname.rstrip('.')}.db"
            abs_zfile = self.state_dir / self.rendered_dir_name / rel_zfile
            bind_type = "master" if zone_type in {"primary", "master"} else "slave"
            allow_update = (
                f'allow-update {{ key "{tsig_key_name}"; }}; ' if tsig_key_name else ""
            )
            if zone_type in {"primary", "master"}:
                self._write_zone_file(new_dir / rel_zfile, zone)
            return (
                f'zone "{zname}" {{ type {bind_type}; file "{abs_zfile}"; '
                f"{allow_update}}};\n"
            )

        def _rpz_stanza(bl: dict[str, Any], file_prefix: str) -> str:
            """Build an RPZ ``zone "..." { ... };`` and write its file.

            Entries render as CNAME records: nxdomain → CNAME .,
            sinkhole → CNAME rpz-drop., redirect → CNAME <target>.,
            exceptions → CNAME rpz-passthru.
            """
            zname = bl["rpz_zone_name"]
            rel = f"zones/{file_prefix}{zname.rstrip('.')}.db"
            abs_zfile = self.state_dir / self.rendered_dir_name / rel
            self._write_rpz_zone_file(new_dir / rel, bl)
            return (
                f'zone "{zname}" {{ type master; file "{abs_zfile}"; '
                f"allow-query {{ localhost; }}; }};\n"
            )

        def _indent(text: str, spaces: int = 4) -> str:
            pad = " " * spaces
            return "".join(
                (pad + ln if ln.strip() else ln)
                for ln in text.splitlines(keepends=True)
            )

        if has_views:
            # Split-horizon (issue #24): every zone + RPZ lives inside its
            # view block. Group-level blocklists (view_name=None) replicate
            # into EVERY view; per-view blocklists land only in their own
            # view. Files are namespaced per view (zones/<view>/...) so
            # identical zone names across views don't collide. Views are
            # already ordered low→high by the control plane for first-match
            # precedence.
            global_bls = [bl for bl in blocklists if bl.get("view_name") is None]
            for view in views:
                vname = view.get("name") or ""
                if not vname:
                    continue
                match_clients = "; ".join(
                    str(c) for c in (view.get("match_clients") or ["any"])
                )
                recursion_v = "yes" if view.get("recursion", True) else "no"
                view_bls = [
                    bl for bl in blocklists if bl.get("view_name") == vname
                ] + global_bls
                body = ""
                if view_bls:
                    zlist = "; ".join(
                        f'zone "{bl["rpz_zone_name"].rstrip(".")}"' for bl in view_bls
                    )
                    body += f"response-policy {{ {zlist}; }} break-dnssec yes;\n"
                for zone in bundle.get("zones", []):
                    if zone.get("view_name") != vname:
                        continue
                    body += _zone_stanza(zone, f"{vname}/")
                for bl in view_bls:
                    body += _rpz_stanza(bl, f"{vname}/")
                md = view.get("match_destinations") or []
                md_line = (
                    f"    match-destinations {{ {'; '.join(str(m) for m in md)}; }};\n"
                    if md
                    else ""
                )
                conf += (
                    f'view "{vname}" {{\n'
                    f"    match-clients {{ {match_clients}; }};\n"
                    f"{md_line}"
                    f"    recursion {recursion_v};\n"
                    f"{_indent(body)}"
                    f"}};\n"
                )
        else:
            for zone in bundle.get("zones", []):
                conf += _zone_stanza(zone, "")

            # BIND9 catalog zone (RFC 9432) — flat path only. Catalog + views
            # is an unsupported combo in this cut (views render their own
            # per-view zones); the producer/consumer wiring assumes top-level
            # zones, which BIND9 forbids alongside views.
            catalog = bundle.get("catalog") or None
            if catalog and catalog.get("mode") == "producer":
                cname = catalog["zone_name"]
                rel = f"zones/{cname.rstrip('.')}.db"
                abs_zfile = self.state_dir / self.rendered_dir_name / rel
                conf += (
                    f'zone "{cname}" {{ type master; file "{abs_zfile}"; '
                    f"allow-transfer {{ any; }}; notify yes; }};\n"
                )
                self._write_catalog_zone_file(new_dir / rel, catalog)
            elif catalog and catalog.get("mode") == "consumer":
                cname = catalog["zone_name"]
                producer_addr = (catalog.get("producer_addr") or "").strip()
                if producer_addr:
                    # `catalog-zones` lives inside options{}; inject before
                    # the closing brace of the options block. ``in-memory
                    # yes`` keeps member zones in RAM (no per-member files).
                    injection = (
                        f"    catalog-zones {{ "
                        f'zone "{cname}" default-masters {{ {producer_addr}; }} '
                        f"in-memory yes; }};"
                    )
                    target = "    check-integrity no;"
                    if target in conf and injection not in conf:
                        conf = conf.replace(target, target + "\n" + injection, 1)

            for bl in blocklists:
                conf += _rpz_stanza(bl, "")

        (new_dir / "named.conf").write_text(conf)

        # TSIG key — written to tsig/ddns.key (stable path).
        # Issue #249 — atomic write so a crash between write_text +
        # chmod doesn't leave a world-readable secret on disk.
        if tsig_keys:
            tsig_dir = self.state_dir / "tsig"
            tsig_dir.mkdir(parents=True, exist_ok=True)
            k = tsig_keys[0]
            tsig_file = tsig_dir / "ddns.key"
            tsig_tmp = tsig_file.with_suffix(".key.new")
            payload = (
                f'key "{k["name"]}" {{ algorithm {k.get("algorithm", "hmac-sha256")}; '
                f'secret "{k["secret"]}"; }};\n'
            )
            fd = os.open(
                str(tsig_tmp),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, payload.encode())
            finally:
                os.close(fd)
            tsig_tmp.replace(tsig_file)

    def _write_catalog_zone_file(self, path: Path, catalog: dict[str, Any]) -> None:
        """Render a BIND9 catalog zone file per RFC 9432.

        Each member zone shows up as a synthetic label
        ``<sha1-of-wire-name>.zones.<catalog>``. The PTR record at that
        label points back to the member zone name. The mandatory
        ``version`` TXT at the apex pins the schema to "2" (the only
        version BIND9 accepts).

        SOA serial uses ``int(time.time())`` because the long-poll only
        delivers a fresh bundle when membership actually changes (the
        catalog block is in the structural ETag); the agent only re-
        renders on bundle change, so each render really is a different
        membership state and consumers will always pull.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        cname = (catalog.get("zone_name") or "").strip()
        if not cname:
            return
        members = catalog.get("members") or []
        serial = int(time.time())
        lines = [
            "$TTL 86400",
            f"@ IN SOA invalid. invalid. ( {serial} 86400 3600 86400 86400 )",
            "@ IN NS invalid.",
            'version IN TXT "2"',
        ]
        for m in members:
            zname = (m.get("zone_name") or "").strip()
            if not zname:
                continue
            text = zname.lower().rstrip(".")
            # RFC 9432 §4.1: hash is SHA-1 of the *wire-format* zone
            # name (each label prefixed with its length byte, root null
            # byte at the end).
            wire = (
                b"".join(
                    bytes([len(label)]) + label.encode("ascii")
                    for label in text.split(".")
                    if label
                )
                + b"\x00"
            )
            digest = hashlib.sha1(wire).hexdigest()
            text_with_dot = text + "." if text else "."
            lines.append(f"{digest}.zones IN PTR {text_with_dot}")
        path.write_text("\n".join(lines) + "\n")

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
            "ns1 IN A 127.0.0.1",
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
            "@ IN SOA localhost. root.localhost. ( 1 3600 600 86400 60 )",
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
        # If start_daemon deferred at boot (no rendered config existed
        # yet), this is the moment we have one — start named now. Without
        # this, a fresh agent that joins a brand-new control plane (no
        # zones yet) never launches the daemon, port 53 stays unbound,
        # and the K8s readiness probe (tcpSocket: 53) never passes.
        if not self.daemon_running():
            self.start_daemon()
            return
        # Signal daemon. Try rndc first; if it isn't configured (no rndc.key),
        # fall back to SIGHUP which named handles as a config + zone reload.
        rndc_ok = False
        if shutil.which("rndc"):
            cmd = ["rndc"]
            agent_conf = self.state_dir / "rndc.conf"
            if agent_conf.exists():
                cmd += ["-c", str(agent_conf)]
            cmd.append("reconfig")
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            rndc_ok = res.returncode == 0
            if not rndc_ok:
                log.warning(
                    "rndc_failed_falling_back_to_sighup", stderr=res.stderr.strip()
                )
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

        # ``rrset_action`` is set by callers that need precise multi-RR
        # semantics (most notably DNS pools, where N A records share a
        # single name and ``replace`` would clobber siblings every time
        # a member is added). When unset (the legacy default) the
        # existing replace/wildcard-delete behaviour applies, matching
        # the single-RR-per-name case the operator-facing record CRUD
        # path was originally written for.
        rrset_action = (rec.get("rrset_action") or "").lower()

        upd = dns.update.Update(zone, keyring=keyring)
        if op["op"] in ("create", "update"):
            if rrset_action == "add":
                upd.add(name, ttl, rtype, wire_value)
            else:
                upd.replace(name, ttl, rtype, wire_value)
        elif op["op"] == "delete":
            if rrset_action == "delete_value":
                # Remove the specific RR only; sibling RRs at the same
                # (name, rtype) survive. Used by pool member removal so
                # taking one member out doesn't drop the rest.
                upd.delete(name, rtype, wire_value)
            else:
                # Some BIND configurations reject the RR-specific delete
                # form (value must exactly match a live RR) when the
                # running daemon has drifted from the zone file. Delete
                # by (name, rtype) so any matching RR gets cleared.
                # Idempotent.
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
        # -f (not -g): keep named in the foreground so subprocess.Popen
        # can track the PID, but honour the user-defined ``logging {}``
        # block in named.conf. ``-g`` *also* runs in the foreground but
        # additionally forces every category to stderr regardless of
        # named.conf — that silently breaks the query-log file channel
        # we render when ``query_log_enabled=True``. We're already
        # running unprivileged as ``spatium`` (entrypoint dropped privs
        # via su-exec), so don't pass ``-u`` — named would try to
        # setgid() to a different user and fail.
        self.daemon_pid = subprocess.Popen(["named", "-f", "-c", str(conf_path)]).pid
        log.info("named_started", pid=self.daemon_pid)

    def daemon_running(self) -> bool:
        if self.daemon_pid is None:
            return False
        try:
            os.kill(self.daemon_pid, 0)
            return True
        except OSError:
            return False

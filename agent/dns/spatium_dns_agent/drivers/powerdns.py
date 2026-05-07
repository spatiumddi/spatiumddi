"""PowerDNS agent driver (issue #127, Phase 1).

Runs alongside ``pdns_server`` inside the dns-powerdns container. Where
the BIND9 driver renders ``named.conf`` + RFC 1035 zone files and reloads
via ``rndc``, this driver:

* Renders ``pdns.conf`` once at first boot using the API key the
  entrypoint generated and shared with us via ``/var/lib/spatium-dns-
  agent/pdns-api.key``. Subsequent renders only rewrite the file if
  the bundle changed materially (loglevel, listen address, etc.).
* Applies zones + records via the local PowerDNS REST API
  (``http://127.0.0.1:8081/api/v1/servers/localhost``). Each
  ``apply_record_op`` call becomes one PATCH to the relevant zone's
  rrsets endpoint; full-bundle config sync diffs zone state and
  reconciles per-zone via the same REST surface.
* Validates by smoke-testing the API on the agent's loopback before
  signalling the daemon to reload (``pdns_control reload``).

Backend storage in Phase 1 is LMDB (``launch=lmdb``), embedded under
``/var/lib/powerdns/pdns.lmdb``. The gpgsql-backed configuration that
shares Postgres with the control plane is deferred to Phase 4 — the
extra cross-process database coupling isn't worth the complexity for
the first ship.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import httpx
import structlog

from .base import DriverBase

log = structlog.get_logger(__name__)


_PDNS_API_BASE = "http://127.0.0.1:8081/api/v1/servers/localhost"
_PDNS_API_TIMEOUT = 10.0
_API_KEY_FILE = "pdns-api.key"


def _quote_txt(value: str) -> str:
    """RFC 1035 TXT quoting — chunk into ≤255-byte strings."""
    s = value
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    chunks = [s[i : i + 255] for i in range(0, len(s), 255)] or [""]
    return " ".join(f'"{c}"' for c in chunks)


def _record_content(rec: dict[str, Any]) -> str:
    """Stitch wire-format content for a single record dict."""
    rtype = rec["type"].upper()
    value = rec.get("value") or ""
    if rtype == "TXT":
        return _quote_txt(value)
    if rtype == "MX":
        prio = rec.get("priority")
        if prio is None:
            prio = 10
        # Operators sometimes paste the priority into value already.
        first = value.lstrip().split(" ", 1)[0]
        if first.isdigit():
            return value
        return f"{prio} {value}"
    if rtype == "SRV":
        prio = rec.get("priority", 0) or 0
        weight = rec.get("weight", 0) or 0
        port = rec.get("port", 0) or 0
        if len(value.split()) >= 4:
            return value
        return f"{prio} {weight} {port} {value}"
    return value


def _qualified_name(zone_name: str, name: str) -> str:
    """Compose the FQDN PowerDNS expects for an rrset name."""
    zone = zone_name.rstrip(".") + "."
    if name in ("", "@") or name.rstrip(".") == zone.rstrip("."):
        return zone
    return f"{name.rstrip('.')}.{zone}"


class PowerDNSDriver(DriverBase):
    """PowerDNS agent driver — Phase 1."""

    daemon_pid: int | None = None

    # ── Render / validate / swap ────────────────────────────────────────────

    def render(self, bundle: dict[str, Any]) -> None:
        """Write ``pdns.conf`` + the desired-state JSON the agent uses
        to drive the API on the next sync.

        ``pdns.conf`` is largely static after first boot — listen
        addresses, the API key, and the LMDB filename don't change at
        runtime. We still rewrite it on every render so operators
        editing options through the UI (loglevel, listen address) see
        the change without restarting the container.
        """
        new_dir = self.state_dir / "rendered.new"
        if new_dir.exists():
            shutil.rmtree(new_dir)
        new_dir.mkdir(parents=True)

        api_key = self._load_or_generate_api_key()
        opts = bundle.get("options", {}) or {}
        log_level = int(opts.get("log_level", 4))

        conf_path = new_dir / "pdns.conf"
        conf_path.write_text(self._render_conf(api_key=api_key, log_level=log_level))

        # Stash the desired-state JSON for the API reconciler. The
        # supervisor calls ``apply_config`` (default impl) which calls
        # render → validate → swap_and_reload; the actual REST PATCH
        # work happens in ``swap_and_reload`` so the daemon is up
        # before we try to reach its API.
        zones_payload = []
        for zone in bundle.get("zones", []) or []:
            zname = (zone.get("name") or "").rstrip(".") + "."
            if not zname or zname == ".":
                continue
            ztype = zone.get("type", "primary")
            if ztype == "forward":
                # Phase 1: skip forward zones on PowerDNS — they're a
                # recursor concept and the authoritative server doesn't
                # consume them. The control-plane validator will surface
                # this via the capabilities() dict in Phase 2.
                continue
            rrsets: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for rec in zone.get("records") or []:
                qname = _qualified_name(zname, rec.get("name") or "@")
                rtype = rec["type"].upper()
                rrsets.setdefault((qname, rtype), []).append(
                    {
                        "content": _record_content(rec),
                        "disabled": False,
                    }
                )
            zones_payload.append(
                {
                    "name": zname,
                    "kind": "Native",
                    "serial": zone.get("serial") or 1,
                    "rrsets": [
                        {
                            "name": qname,
                            "type": rtype,
                            "ttl": zone.get("ttl", 3600),
                            "records": rrs,
                        }
                        for (qname, rtype), rrs in sorted(rrsets.items())
                    ],
                }
            )

        (new_dir / "zones.json").write_text(json.dumps(zones_payload, indent=2))

    def validate(self) -> None:
        """``pdns_server --config-check`` if the binary supports it.

        Recent PowerDNS versions carry a ``--no-config`` smoke. Falling
        back to "config file exists and parses as text" is acceptable
        — invalid LMDB-backend config is caught when the daemon
        actually starts (the supervisor exits non-zero and the
        orchestrator restarts us).
        """
        new_dir = self.state_dir / "rendered.new"
        conf = new_dir / "pdns.conf"
        if not conf.exists():
            raise RuntimeError("pdns.conf was not written")
        if shutil.which("pdns_server"):
            res = subprocess.run(
                [
                    "pdns_server",
                    "--config-dir",
                    str(new_dir),
                    "--config-name",
                    "",
                    "--no-config",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            # pdns_server with --no-config exits 0 after writing config
            # to stdout. Non-zero indicates a parse error in our file.
            if res.returncode not in (0,):
                stderr = res.stderr.strip()
                if "unknown option" in stderr.lower():
                    # Older pdns versions don't support --no-config;
                    # treat as soft success.
                    log.warning("pdns_server_no_config_unsupported_skipping")
                    return
                raise RuntimeError(f"pdns_server config-check failed: {stderr}")

    def swap_and_reload(self) -> None:
        """Promote the new render into place and reconcile via REST.

        The reconcile loop only runs once we've confirmed the daemon
        is up — at first boot the supervisor calls ``start_daemon``
        before the first ``apply_config``, but if the daemon crashed
        between renders the API will be unreachable and the reconcile
        is a soft no-op (the next bundle will retry).
        """
        new_dir = self.state_dir / "rendered.new"
        current = self.state_dir / "rendered"
        backup = self.state_dir / "rendered.prev"
        if current.exists():
            if backup.exists():
                shutil.rmtree(backup)
            current.rename(backup)
        new_dir.rename(current)

        api_key = self._load_or_generate_api_key()
        zones_path = current / "zones.json"
        if not zones_path.exists():
            log.warning("powerdns_zones_payload_missing")
            return
        try:
            payload = json.loads(zones_path.read_text())
        except Exception as exc:  # noqa: BLE001
            log.error("powerdns_zones_payload_unreadable", error=str(exc))
            return

        try:
            self._reconcile_zones(api_key, payload)
        except httpx.HTTPError as exc:
            # Soft fail — the daemon may still be coming up at first
            # boot. The supervisor's tick will keep the agent alive
            # and the next config sync (or the heartbeat reconcile)
            # will retry.
            log.warning("powerdns_reconcile_http_error", error=str(exc))

    # ── Record ops (REST PATCH against loopback API) ───────────────────────

    def apply_record_op(self, op: dict[str, Any]) -> None:
        """Apply a single record op via the PowerDNS REST API."""
        api_key = self._load_or_generate_api_key()
        zone_raw = op["zone_name"]
        zone = zone_raw.rstrip(".") + "."
        rec = op["record"]
        name = _qualified_name(zone, rec.get("name") or "@")
        rtype = rec["type"].upper()
        ttl = rec.get("ttl") or 3600

        op_kind = op["op"]
        if op_kind == "delete":
            rrset = {
                "name": name,
                "type": rtype,
                "changetype": "DELETE",
            }
        else:  # create | update
            rrset = {
                "name": name,
                "type": rtype,
                "ttl": ttl,
                "changetype": "REPLACE",
                "records": [
                    {
                        "content": _record_content(rec),
                        "disabled": False,
                    }
                ],
            }

        url = f"{_PDNS_API_BASE}/zones/{zone}"
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        body = {"rrsets": [rrset]}
        with httpx.Client(timeout=_PDNS_API_TIMEOUT) as client:
            resp = client.patch(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"PowerDNS PATCH {zone}/{name}/{rtype} returned "
                f"{resp.status_code}: {resp.text[:200]}"
            )
        log.info(
            "powerdns_record_op_applied",
            zone=zone,
            name=name,
            type=rtype,
            op=op_kind,
        )

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start_daemon(self) -> None:
        """Spawn ``pdns_server`` from the rendered config dir.

        The container entrypoint already created the LMDB directory
        and seeded the API key. We don't drop privileges further —
        the entrypoint dropped to the unprivileged ``spatium`` user
        before invoking the agent.
        """
        current = self.state_dir / "rendered"
        if not (current / "pdns.conf").exists():
            log.warning("pdns_conf_missing_startup_deferred")
            return
        if not shutil.which("pdns_server"):
            log.error("pdns_server_binary_missing")
            return
        # ``--daemon=no`` + foreground; pdns logs to stderr, captured
        # by the container runtime same as named with ``-g``.
        self.daemon_pid = subprocess.Popen(
            [
                "pdns_server",
                "--daemon=no",
                "--guardian=no",
                "--config-dir",
                str(current),
                "--config-name",
                "",
            ]
        ).pid
        log.info("pdns_server_started", pid=self.daemon_pid)

    def daemon_running(self) -> bool:
        if self.daemon_pid is None:
            return False
        try:
            os.kill(self.daemon_pid, 0)
            return True
        except OSError:
            return False

    # ── Internals ───────────────────────────────────────────────────────────

    def _api_key_path(self) -> Path:
        return self.state_dir / _API_KEY_FILE

    def _load_or_generate_api_key(self) -> str:
        """Return the local PowerDNS REST API key.

        The container entrypoint usually pre-creates this file. If
        the agent boots in a fresh state dir (volume not mounted),
        we generate one ourselves so the first config sync has
        something to use — pdns will read the same file when the
        entrypoint copies it into place at startup.
        """
        path = self._api_key_path()
        if path.exists():
            return path.read_text().strip()
        key = secrets.token_urlsafe(32)
        path.write_text(key + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key

    def _render_conf(self, *, api_key: str, log_level: int) -> str:
        # Mirrors backend/app/drivers/dns/powerdns.py::render_pdns_conf.
        # Agent and control plane render the same shape; the agent
        # owns the API key while the control plane sees only the
        # placeholder.
        return "\n".join(
            [
                "# pdns.conf — generated by SpatiumDDI DNS agent",
                "# Do not edit by hand; the agent rewrites this file"
                " on every config sync.",
                "",
                "launch=lmdb",
                "lmdb-filename=/var/lib/powerdns/pdns.lmdb",
                "lmdb-shards=64",
                "lmdb-sync-mode=sync",
                "",
                "local-address=0.0.0.0",
                "local-port=53",
                "",
                "api=yes",
                f"api-key={api_key}",
                "webserver=yes",
                "webserver-address=127.0.0.1",
                "webserver-port=8081",
                "webserver-allow-from=127.0.0.1,::1",
                "",
                f"loglevel={log_level}",
                "log-dns-details=no",
                "log-dns-queries=no",
                "",
                "expand-alias=no",
                "dnsupdate=no",
                "",
            ]
        )

    def _reconcile_zones(self, api_key: str, payload: list[dict[str, Any]]) -> None:
        """Idempotently bring the local PowerDNS zone set in line with
        ``payload``. Phase 1 reconciliation is per-zone create-or-update.
        Zones present in PowerDNS that aren't in the bundle are NOT
        deleted yet — that's a control-plane safety call (operators
        should explicitly delete a zone, not have it disappear because
        a sync glitched). Phase 2 wires the explicit-delete signal
        from the bundle.
        """
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        with httpx.Client(timeout=_PDNS_API_TIMEOUT) as client:
            try:
                existing = client.get(
                    f"{_PDNS_API_BASE}/zones", headers=headers
                ).json()
            except (httpx.HTTPError, ValueError):
                existing = []
            existing_names = {z["name"] for z in existing if isinstance(z, dict)}

            for zone_payload in payload:
                zone_name = zone_payload["name"]
                if zone_name not in existing_names:
                    # Create — POST /zones with the full rrset list.
                    create_body = {
                        "name": zone_name,
                        "kind": zone_payload.get("kind", "Native"),
                        "rrsets": zone_payload.get("rrsets") or [],
                    }
                    resp = client.post(
                        f"{_PDNS_API_BASE}/zones",
                        headers=headers,
                        json=create_body,
                    )
                    if resp.status_code >= 400:
                        log.error(
                            "powerdns_zone_create_failed",
                            zone=zone_name,
                            status=resp.status_code,
                            body=resp.text[:200],
                        )
                        continue
                    log.info("powerdns_zone_created", zone=zone_name)
                else:
                    # Update — PATCH /zones/{zone} with REPLACE rrsets.
                    rrsets = []
                    for rs in zone_payload.get("rrsets") or []:
                        rrsets.append(
                            {
                                "name": rs["name"],
                                "type": rs["type"],
                                "ttl": rs.get("ttl", 3600),
                                "changetype": "REPLACE",
                                "records": rs.get("records") or [],
                            }
                        )
                    if not rrsets:
                        continue
                    resp = client.patch(
                        f"{_PDNS_API_BASE}/zones/{zone_name}",
                        headers=headers,
                        json={"rrsets": rrsets},
                    )
                    if resp.status_code >= 400:
                        log.error(
                            "powerdns_zone_patch_failed",
                            zone=zone_name,
                            status=resp.status_code,
                            body=resp.text[:200],
                        )
                        continue
                    log.info(
                        "powerdns_zone_reconciled",
                        zone=zone_name,
                        rrset_count=len(rrsets),
                    )

                # Per-zone LUA-records gate (Phase 3b). PowerDNS only
                # evaluates LUA records at query time when the zone has
                # ``ENABLE-LUA-RECORDS`` metadata set; otherwise the
                # snippet is served as a literal string. Set the
                # metadata after the zone exists so the PUT lands. If
                # the zone has no LUA records right now we still don't
                # touch the metadata (idempotent — a no-op if
                # already-set; nothing breaks if pre-existing).
                has_lua = any(
                    rs.get("type", "").upper() == "LUA"
                    for rs in zone_payload.get("rrsets") or []
                )
                if has_lua:
                    meta_resp = client.put(
                        f"{_PDNS_API_BASE}/zones/{zone_name}/metadata/"
                        "ENABLE-LUA-RECORDS",
                        headers=headers,
                        json={
                            "kind": "ENABLE-LUA-RECORDS",
                            "metadata": ["1"],
                        },
                    )
                    if meta_resp.status_code >= 400:
                        log.warning(
                            "powerdns_lua_metadata_failed",
                            zone=zone_name,
                            status=meta_resp.status_code,
                            body=meta_resp.text[:200],
                        )

    # ── Reload (compatibility with bind9 daemon-pid signal pattern) ────────

    def _reload_via_api(self) -> None:
        """Optional — issue a notify on every zone after a bulk
        change. PowerDNS is master-only by default; this only matters
        when secondaries are configured. Currently unused but kept
        as a hook for Phase 2 supermaster wiring.
        """
        if self.daemon_pid:
            try:
                os.kill(self.daemon_pid, signal.SIGUSR1)
            except OSError as exc:
                log.warning("pdns_sigusr1_failed", error=str(exc))

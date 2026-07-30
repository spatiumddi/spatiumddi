"""Technitium DNS Server agent driver (v1 — primary zones + record CRUD).

Runs alongside the Technitium ``DnsServerApp`` process inside the
dns-technitium container. Unlike BIND9 (named.conf + RFC 1035 zone files +
``rndc``) or PowerDNS (``pdns.conf`` + REST reconcile), Technitium has **no
on-disk config file this driver manages at all** — the daemon persists its
own config under ``/etc/dns`` and is configured entirely over its HTTP API
(``http://127.0.0.1:5380/api/...``). So this driver:

* Provisions a permanent API token on first-ever boot. The container image
  bakes ``DNS_SERVER_ADMIN_PASSWORD`` with a value this driver generates,
  which Technitium consumes to set the ``admin`` user's password the very
  first time ``/etc/dns`` is empty. The driver then calls
  ``/api/user/createToken`` ONCE and persists the resulting bearer token —
  calling ``createToken`` again would mint a second, orphaned token on the
  server (confirmed empirically: the endpoint is not idempotent), so the
  local token file is the source of truth once it exists.
* Applies zone + record state via the REST API. ``render()``/``validate()``/
  ``swap_and_reload()`` collapse into: stash the desired-state JSON, then
  reconcile it against the live API in ``swap_and_reload()`` (same split as
  PowerDNS, for symmetry with the rest of the codebase, even though there's
  no config file being swapped here).
* Zone apex NS/SOA are Technitium-managed (auto-created on
  ``/api/zones/create``) and are NOT pushed from the bundle — confirmed the
  daemon renders its own SOA + one NS record at zone creation, so treating
  the bundle's NS/SOA as authoritative would just create duplicate/foreign
  NS records alongside Technitium's own.

v1 scope: primary zones only. Record types: A, AAAA, CNAME, MX, TXT, NS
(zone-referral records off-apex only — apex NS is daemon-managed), PTR, SRV,
CAA, TLSA, SSHFP, NAPTR, URI, DNAME, SVCB, HTTPS (SVCB/HTTPS best-effort —
see ``_svcb_params`` for the one documented limitation: multi-value params
like ``alpn="h2,h3"`` only carry the first value through, logged as a
warning).

Deferred to fast-follow phases: DNSSEC (``/api/zones/dnssec/*`` — same
shape as PowerDNS's, should port quickly), native DoT/DoH/DoQ listener
wiring (Technitium's real differentiator — no dnsdist-style sidecar needed,
unlike PowerDNS), catalog zones, secondary/stub zones.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from ._process import find_running_daemon, is_zombie
from .base import DriverBase

log = structlog.get_logger(__name__)


_API_BASE = "http://127.0.0.1:5380/api"
_API_TIMEOUT = 10.0
_ADMIN_PASSWORD_FILE = "technitium-admin-password"
_API_TOKEN_FILE = "technitium-api.token"
_TOKEN_NAME = "spatiumddi-agent"

# Record types whose zone-apex form is daemon-managed (SOA always; NS only
# at the apex — an off-apex NS is a legitimate delegation record and IS
# reconciled normally).
_DAEMON_MANAGED_APEX_TYPES = frozenset({"SOA"})


def _qualified_name(zone_name: str, name: str) -> str:
    """Compose the bare (no trailing dot) FQDN Technitium expects for a
    record's ``domain`` param."""
    zone = zone_name.rstrip(".")
    bare = (name or "@").rstrip(".")
    if bare in ("", "@") or bare == zone:
        return zone
    return f"{bare}.{zone}"


def _svcb_params(value: str) -> tuple[int, str, str]:
    """Parse a BIND-zone-file-style SVCB/HTTPS rdata string into
    ``(priority, target, svcParams)`` for the Technitium API.

    Input shape (matches what the control-plane driver + BIND9 render,
    e.g. ``'1 . alpn="h2,h3"'``): priority, target, then space-separated
    ``key=value`` params with optionally-quoted values.

    Known limitation: Technitium's ``svcParams`` wire format
    (``key|value`` pairs) does not accept a comma-separated multi-value
    single param the way BIND's zone-file rdata does (confirmed
    empirically — ``alpn|h2,h3`` errors). Only the first value of a
    multi-value param is carried through; the rest are dropped with a
    warning. Single-value params (the common case) round-trip exactly.
    """
    tokens = shlex.split(value)
    if len(tokens) < 2:
        return (1, ".", "")
    priority = int(tokens[0]) if tokens[0].isdigit() else 1
    target = tokens[1]
    parts = []
    for tok in tokens[2:]:
        if "=" not in tok:
            continue
        key, _, raw_val = tok.partition("=")
        first_val = raw_val.split(",", 1)[0]
        if "," in raw_val:
            log.warning(
                "technitium_svcb_multivalue_param_truncated",
                key=key,
                dropped=raw_val.split(",", 1)[1],
            )
        parts.append(f"{key}|{first_val}")
    return (priority, target, ",".join(parts))


def _record_params(rtype: str, value: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Build the type-specific param dict for
    ``/api/zones/records/{add,delete}`` — shared by both endpoints since
    ``delete`` requires the exact same value params to identify the record.
    """
    value = value.rstrip(".")
    if rtype in ("A", "AAAA"):
        return {"ipAddress": value}
    if rtype == "CNAME":
        return {"cname": value}
    if rtype == "DNAME":
        return {"dname": value}
    if rtype == "NS":
        return {"nameServer": value}
    if rtype == "PTR":
        return {"ptrName": value}
    if rtype == "MX":
        return {"exchange": value, "preference": rec.get("priority") or 10}
    if rtype == "SRV":
        return {
            "target": value,
            "priority": rec.get("priority") or 0,
            "weight": rec.get("weight") or 0,
            "port": rec.get("port") or 0,
        }
    if rtype == "TXT":
        return {"text": value}
    if rtype == "CAA":
        # value shape: "<flags> <tag> <target>", e.g. '0 issue "letsencrypt.org"'
        tokens = shlex.split(value)
        flags = int(tokens[0]) if tokens and tokens[0].isdigit() else 0
        tag = tokens[1] if len(tokens) > 1 else "issue"
        target = tokens[2] if len(tokens) > 2 else ""
        return {"flags": flags, "tag": tag, "value": target}
    if rtype == "TLSA":
        tokens = shlex.split(value)
        return {
            "tlsaCertificateUsage": tokens[0] if len(tokens) > 0 else "0",
            "tlsaSelector": tokens[1] if len(tokens) > 1 else "0",
            "tlsaMatchingType": tokens[2] if len(tokens) > 2 else "0",
            "tlsaCertificateAssociationData": tokens[3] if len(tokens) > 3 else "",
        }
    if rtype == "SSHFP":
        tokens = shlex.split(value)
        return {
            "sshfpAlgorithm": tokens[0] if len(tokens) > 0 else "0",
            "sshfpFingerprintType": tokens[1] if len(tokens) > 1 else "0",
            "sshfpFingerprint": tokens[2] if len(tokens) > 2 else "",
        }
    if rtype == "NAPTR":
        tokens = shlex.split(value)
        return {
            "naptrOrder": tokens[0] if len(tokens) > 0 else "0",
            "naptrPreference": tokens[1] if len(tokens) > 1 else "0",
            "naptrFlags": tokens[2] if len(tokens) > 2 else "",
            "naptrServices": tokens[3] if len(tokens) > 3 else "",
            "naptrRegexp": tokens[4] if len(tokens) > 4 else "",
            "naptrReplacement": tokens[5] if len(tokens) > 5 else ".",
        }
    if rtype == "URI":
        tokens = shlex.split(value)
        return {
            "uriPriority": tokens[0] if len(tokens) > 0 else "1",
            "uriWeight": tokens[1] if len(tokens) > 1 else "1",
            "uri": tokens[2] if len(tokens) > 2 else "",
        }
    if rtype in ("SVCB", "HTTPS"):
        priority, target, params = _svcb_params(value)
        out: dict[str, Any] = {"svcPriority": priority, "svcTargetName": target}
        if params:
            out["svcParams"] = params
        return out
    # Unrecognised type — pass the raw value through under a best-guess key
    # so the API's own error message tells us what's missing, rather than
    # silently dropping the record.
    return {"value": value}


class TechnitiumDriver(DriverBase):
    """Technitium agent driver — v1."""

    daemon_pid: int | None = None

    # ── Render / validate / swap ────────────────────────────────────────────

    def render(self, bundle: dict[str, Any]) -> None:
        """Stash the desired-state JSON for the API reconciler.

        There is no config file to write for this driver — Technitium's
        own on-disk state under ``/etc/dns`` is entirely daemon-managed.
        The bundle-to-JSON step exists purely so ``swap_and_reload`` has a
        stable, atomically-swapped snapshot to reconcile against (matches
        the render → validate → swap_and_reload shape every driver shares).
        """
        new_dir = self.state_dir / "rendered.new"
        if new_dir.exists():
            shutil.rmtree(new_dir)
        new_dir.mkdir(parents=True)

        zones_payload = []
        for zone in bundle.get("zones", []) or []:
            zname = (zone.get("name") or "").rstrip(".")
            if not zname:
                continue
            ztype = zone.get("type", "primary")
            if ztype != "primary":
                log.warning(
                    "technitium_zone_type_unsupported_v1",
                    zone=zname,
                    zone_type=ztype,
                )
                continue
            records = []
            for rec in zone.get("records") or []:
                rtype = (rec.get("type") or "").upper()
                if rtype in _DAEMON_MANAGED_APEX_TYPES:
                    continue
                name = _qualified_name(zname, rec.get("name") or "@")
                if rtype == "NS" and name == zname:
                    # Apex NS is daemon-managed (created at zone-create
                    # time, pointed at the container's own hostname).
                    # Off-apex NS (delegations) are handled normally.
                    continue
                records.append(
                    {
                        "domain": name,
                        "type": rtype,
                        "ttl": rec.get("ttl") or zone.get("ttl") or 3600,
                        **_record_params(rtype, rec.get("value") or "", rec),
                    }
                )
            zones_payload.append({"zone": zname, "type": "Primary", "records": records})

        if bundle.get("blocklists"):
            log.warning(
                "technitium_blocklists_unsupported",
                blocklist_count=len(bundle["blocklists"]),
                hint=(
                    "Wiring SpatiumDDI's blocklist model to Technitium's "
                    "native blocking apps is a fast-follow, not v1 scope."
                ),
            )

        (new_dir / "zones.json").write_text(json.dumps(zones_payload, indent=2))

    def validate(self) -> None:
        new_dir = self.state_dir / "rendered.new"
        zones_path = new_dir / "zones.json"
        if not zones_path.exists():
            raise RuntimeError("zones.json was not written")
        try:
            json.loads(zones_path.read_text())
        except ValueError as exc:
            raise RuntimeError(f"zones.json is not valid JSON: {exc}") from exc

    def swap_and_reload(self) -> None:
        """Promote the new render into place and reconcile via REST.

        Cold-boot ordering mirrors PowerDNS: the supervisor calls
        ``start_daemon`` before the first ``apply_config``, so on first
        boot the daemon may still be initializing its web server when we
        get here. Wait for the API before reconciling — otherwise the
        first call fails with connection-refused, the reconcile silently
        gives up, and the structural etag has already advanced so the
        sync loop never retries.
        """
        new_dir = self.state_dir / "rendered.new"
        current = self.state_dir / "rendered"
        backup = self.state_dir / "rendered.prev"
        if current.exists():
            if backup.exists():
                shutil.rmtree(backup)
            current.rename(backup)
        new_dir.rename(current)

        if not self.daemon_running():
            log.info("technitium_daemon_starting_after_first_render")
            self.start_daemon()
        self._wait_for_api_up()

        zones_path = current / "zones.json"
        try:
            payload = json.loads(zones_path.read_text())
        except Exception as exc:  # noqa: BLE001
            log.error("technitium_zones_payload_unreadable", error=str(exc))
            return

        token = self._get_api_token()
        if token is None:
            log.error("technitium_reconcile_skipped_no_token")
            return
        self._reconcile_zones(token, payload)

    def _wait_for_api_up(self, *, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        with httpx.Client(timeout=1.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = client.get(f"{_API_BASE}/user/session/get")
                    if resp.status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
        log.warning("technitium_api_wait_timeout", timeout_s=timeout_s)

    # ── Record ops (REST calls against loopback API) ────────────────────────

    def apply_record_op(self, op: dict[str, Any]) -> dict[str, Any] | None:
        """Apply a single record op via the Technitium REST API.

        ``create``/``update`` both map to ``/api/zones/records/add`` with
        ``overwrite=false`` — Technitium's op payload only carries the NEW
        value (never the old one), so a value *change* on ``update``
        has to be expressed as an rrset REPLACE, not an append —
        exactly what BIND9's driver does with
        ``dns.update.Update.replace`` and PowerDNS's does with an
        rrset ``REPLACE`` PATCH. Technitium's equivalent is
        ``overwrite=true`` on ``/api/zones/records/add``, which wipes
        the rrset at ``(domain, type)`` and writes the new value + TTL.

        ``rrset_action`` is the project-wide per-op override for that
        default (see ``bind9.py``): DNS pools set ``"add"`` because N
        A records share one name there and a REPLACE would clobber
        siblings every time a member is added. Honour it here too, or
        pool members would delete each other.

        Do NOT revert this to an unconditional ``overwrite=false``:
        appending on every ``update`` makes an edited record serve the
        OLD and NEW values simultaneously, and makes a TTL-only edit a
        silent no-op ("already exists"). Neither self-heals, because
        record CRUD bumps the bundle's ``etag`` but not its
        ``structural_etag``, so ``swap_and_reload``'s full-zone
        reconcile never runs on a record edit (see ``sync.py``).
        """
        token = self._get_api_token()
        if token is None:
            raise RuntimeError("no Technitium API token available")

        zone = op["zone_name"].rstrip(".")
        op_kind = op["op"]
        rec = op["record"]
        rtype = (rec.get("type") or "").upper()
        name = _qualified_name(zone, rec.get("name") or "@")
        ttl = rec.get("ttl") or 3600
        params = {
            "domain": name,
            "zone": zone,
            "type": rtype,
            **_record_params(rtype, rec.get("value") or "", rec),
        }

        rrset_action = (rec.get("rrset_action") or "").lower()
        endpoint = "delete" if op_kind == "delete" else "add"
        if op_kind != "delete":
            params["ttl"] = ttl
            # REPLACE the rrset by default (matches bind9's ``upd.replace``
            # and PowerDNS's rrset REPLACE); append only when the caller
            # explicitly asks for sibling-preserving semantics.
            params["overwrite"] = "false" if rrset_action == "add" else "true"

        resp = self._call(token, "POST", f"zones/records/{endpoint}", params)
        body = resp.json()
        if body.get("status") == "error":
            msg = (body.get("errorMessage") or "").lower()
            # Idempotent no-ops: retried create of an identical record, or
            # a delete of a record that's already gone.
            if "already exists" in msg or "no such record" in msg:
                log.info(
                    "technitium_record_op_idempotent_noop",
                    zone=zone,
                    name=name,
                    type=rtype,
                    op=op_kind,
                    detail=body.get("errorMessage"),
                )
                return None
            raise RuntimeError(
                f"Technitium {endpoint} {zone}/{name}/{rtype} failed: "
                f"{body.get('errorMessage')}"
            )
        log.info(
            "technitium_record_op_applied", zone=zone, name=name, type=rtype, op=op_kind
        )
        return None

    def _reconcile_zones(self, token: str, payload: list[dict[str, Any]]) -> None:
        """Bring each zone's record set in line with ``payload`` via a
        full per-zone diff: create the zone if missing, then delete
        records present on the daemon but absent from desired state and
        add records present in desired state but absent on the daemon.

        Zones present on the daemon but absent from ``payload`` are NOT
        deleted — same safety stance as the other drivers (operators
        delete a zone explicitly, it doesn't disappear from a sync
        glitch).
        """
        for zone_payload in payload:
            zone = zone_payload["zone"]
            self._ensure_zone_exists(token, zone)

            existing = self._get_zone_records(token, zone)
            desired = zone_payload.get("records") or []

            def _fingerprint(rec: dict[str, Any]) -> tuple[Any, ...]:
                extra = tuple(
                    sorted(
                        (k, str(v))
                        for k, v in rec.items()
                        if k not in ("domain", "type", "ttl", "zone")
                    )
                )
                # TTL participates: a TTL-only edit is a real desired-state
                # change, and this reconcile is the only path that can
                # converge it (the incremental op path can't see the old
                # value). Compared as int so a JSON "300" from the daemon
                # doesn't read as different from a rendered 300. Values are
                # str()-normalised for the same reason — Technitium returns
                # some rdata fields as numbers/enums where our add-params
                # are strings, and a bare type mismatch would make every
                # such record look "changed" on every single pass.
                ttl = rec.get("ttl")
                return (
                    rec.get("domain"),
                    rec.get("type"),
                    int(ttl) if ttl is not None else None,
                    extra,
                )

            existing_by_fp = {_fingerprint(r): r for r in existing}
            desired_by_fp = {_fingerprint(r): r for r in desired}

            to_delete = [r for fp, r in existing_by_fp.items() if fp not in desired_by_fp]
            to_add = [r for fp, r in desired_by_fp.items() if fp not in existing_by_fp]

            for rec in to_delete:
                if rec.get("type") in _DAEMON_MANAGED_APEX_TYPES:
                    continue
                if rec.get("type") == "NS" and rec.get("domain") == zone:
                    continue
                resp = self._call(
                    token,
                    "POST",
                    "zones/records/delete",
                    {
                        "domain": rec["domain"],
                        "zone": zone,
                        "type": rec["type"],
                        **{
                            k: v
                            for k, v in rec.items()
                            if k not in ("domain", "type", "ttl", "zone")
                        },
                    },
                )
                body = resp.json()
                if body.get("status") == "error" and "no such record" not in (
                    body.get("errorMessage") or ""
                ).lower():
                    log.warning(
                        "technitium_reconcile_delete_failed",
                        zone=zone,
                        record=rec,
                        error=body.get("errorMessage"),
                    )

            for rec in to_add:
                resp = self._call(
                    token,
                    "POST",
                    "zones/records/add",
                    {**rec, "zone": zone, "overwrite": "false"},
                )
                body = resp.json()
                if body.get("status") == "error" and "already exists" not in (
                    body.get("errorMessage") or ""
                ).lower():
                    log.error(
                        "technitium_reconcile_add_failed",
                        zone=zone,
                        record=rec,
                        error=body.get("errorMessage"),
                    )
                    continue
            if to_add or to_delete:
                log.info(
                    "technitium_zone_reconciled",
                    zone=zone,
                    added=len(to_add),
                    deleted=len(to_delete),
                )

    def _ensure_zone_exists(self, token: str, zone: str) -> None:
        resp = self._call(
            token, "POST", "zones/create", {"zone": zone, "type": "Primary"}
        )
        body = resp.json()
        if body.get("status") == "error":
            if "already exists" in (body.get("errorMessage") or "").lower():
                return
            log.error(
                "technitium_zone_create_failed", zone=zone, error=body.get("errorMessage")
            )

    def _get_zone_records(self, token: str, zone: str) -> list[dict[str, Any]]:
        resp = self._call(
            token,
            "GET",
            "zones/records/get",
            {"domain": zone, "zone": zone, "listZone": "true"},
        )
        try:
            body = resp.json()
        except ValueError:
            return []
        if body.get("status") != "ok":
            return []
        out = []
        for rec in body.get("response", {}).get("records") or []:
            rtype = rec.get("type")
            if rtype in _DAEMON_MANAGED_APEX_TYPES:
                continue
            flat = {"domain": rec.get("name"), "type": rtype, "ttl": rec.get("ttl")}
            flat.update(rec.get("rData") or {})
            # Technitium's TXT rData carries extra derived fields
            # (splitText/characterStrings/characterStringsBase64) that
            # never round-trip through our add params — drop them so the
            # fingerprint comparison in ``_reconcile_zones`` doesn't treat
            # every existing TXT record as "different from desired" on
            # every single reconcile pass.
            for extra_key in (
                "splitText",
                "characterStrings",
                "characterStringsBase64",
                "autoIpv4Hint",
                "autoIpv6Hint",
            ):
                flat.pop(extra_key, None)
            out.append(flat)
        return out

    # ── Auth (permanent API token) ──────────────────────────────────────────

    def _admin_password_path(self) -> Path:
        return self.state_dir / _ADMIN_PASSWORD_FILE

    def _api_token_path(self) -> Path:
        return self.state_dir / _API_TOKEN_FILE

    def admin_bootstrap_password(self) -> str:
        """Return the admin password used to initialise the daemon on its
        very first start (via the ``DNS_SERVER_ADMIN_PASSWORD`` env var
        passed to the subprocess). Generated once, persisted like the
        PowerDNS API key (atomic O_NOFOLLOW write, 0600) — needed again
        any time the local API token is lost and must be re-derived via
        ``/api/user/createToken``.
        """
        return self._read_or_create_secret(self._admin_password_path())

    def _get_api_token(self) -> str | None:
        path = self._api_token_path()
        if path.exists():
            try:
                return path.read_text().strip()
            except PermissionError as exc:
                raise RuntimeError(
                    f"Technitium API token file at {path} exists but is "
                    "unreadable by the agent. Fix ownership/permissions "
                    "(should be spatium:spatium 0600)."
                ) from exc
        return self._create_api_token()

    def _create_api_token(self) -> str | None:
        """Exchange the admin bootstrap password for a permanent API
        token via ``/api/user/createToken`` — called at most once ever
        per state dir, since the endpoint mints a NEW token on every
        call (confirmed empirically: it is not idempotent on
        ``tokenName``, so retrying here would silently accumulate
        orphaned tokens on the server).
        """
        password = self.admin_bootstrap_password()
        try:
            resp = httpx.get(
                f"{_API_BASE}/user/createToken",
                params={"user": "admin", "pass": password, "tokenName": _TOKEN_NAME},
                timeout=_API_TIMEOUT,
            )
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("technitium_create_token_failed", error=str(exc))
            return None
        if body.get("status") != "ok" or not body.get("token"):
            log.error(
                "technitium_create_token_rejected", error=body.get("errorMessage")
            )
            return None
        token = body["token"]
        self._write_secret(self._api_token_path(), token)
        log.info("technitium_api_token_created")
        return token

    @staticmethod
    def _write_secret(path: Path, value: str) -> None:
        tmp = path.with_suffix(path.suffix + ".new")
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(fd, (value + "\n").encode())
        finally:
            os.close(fd)
        tmp.replace(path)

    def _read_or_create_secret(self, path: Path) -> str:
        if path.exists():
            try:
                return path.read_text().strip()
            except PermissionError as exc:
                raise RuntimeError(
                    f"Secret file at {path} exists but is unreadable by "
                    "the agent. Fix ownership/permissions (should be "
                    "spatium:spatium 0600)."
                ) from exc
        value = secrets.token_urlsafe(24)
        self._write_secret(path, value)
        return value

    def _call(
        self, token: str, method: str, path: str, params: dict[str, Any]
    ) -> httpx.Response:
        """Issue one API call, auth'd via ``Authorization: Bearer``.

        Technitium returns HTTP 200 even on an invalid/expired token —
        the failure surfaces only in the JSON body's ``status`` field
        (confirmed empirically: ``{"status": "invalid-token", ...}`` at
        HTTP 200). Callers must inspect ``.json()["status"]``, not the
        HTTP status code, to detect auth failure.

        On ``invalid-token`` the cached token is re-provisioned and the
        call retried once. The state dir (which holds the token) and
        ``/etc/dns`` (which holds the daemon's admin account) are
        SEPARATE volumes in every deployment shape, so they can desync —
        wipe the daemon's config to reset zones and the cached token now
        points at an account that no longer exists. Without this the
        agent wedges permanently: every call 200s with ``invalid-token``,
        ``_get_zone_records`` reads it as an empty zone, every add fails,
        and nothing ever re-bootstraps. Mirrors the agent↔control-plane
        401/404 re-bootstrap contract (CLAUDE.md cross-cutting #3).
        """
        resp = self._request(token, method, path, params)
        if self._is_invalid_token(resp):
            fresh = self._reprovision_token(token)
            if fresh is not None:
                log.info("technitium_api_token_reprovisioned", path=path)
                resp = self._request(fresh, method, path, params)
        return resp

    def _request(
        self, token: str, method: str, path: str, params: dict[str, Any]
    ) -> httpx.Response:
        with httpx.Client(timeout=_API_TIMEOUT) as client:
            headers = self._auth_header(token)
            if method == "GET":
                return client.get(f"{_API_BASE}/{path}", params=params, headers=headers)
            return client.post(f"{_API_BASE}/{path}", data=params, headers=headers)

    @staticmethod
    def _is_invalid_token(resp: httpx.Response) -> bool:
        try:
            return bool(resp.json().get("status") == "invalid-token")
        except ValueError:
            # Non-JSON body — not an auth answer; let the caller surface it.
            return False

    def _reprovision_token(self, stale: str) -> str | None:
        """Drop the cached token and mint a fresh one.

        Re-reads the file first: several calls in one reconcile pass hold
        the same stale token in a local, so without this each would mint
        its own replacement and orphan the rest server-side
        (``createToken`` is not idempotent on ``tokenName``).
        """
        path = self._api_token_path()
        try:
            current = path.read_text().strip() if path.exists() else None
        except OSError:
            current = None
        if current is not None and current != stale:
            return current
        path.unlink(missing_ok=True)
        return self._create_api_token()

    @staticmethod
    def _auth_header(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def start_daemon(self) -> None:
        """Spawn ``dotnet DnsServerApp.dll /etc/dns``.

        The bootstrap admin password is passed via
        ``DNS_SERVER_ADMIN_PASSWORD`` on every start — Technitium only
        consumes it the very first time ``/etc/dns`` is empty (fresh
        config), so this is a harmless no-op on every subsequent start.
        """
        if not shutil.which("dotnet"):
            log.error("technitium_dotnet_binary_missing")
            return
        existing = find_running_daemon("dotnet")
        if existing is not None:
            self.daemon_pid = existing
            log.info(
                "technitium_already_running_adopted",
                pid=existing,
                note="did not spawn a second daemon",
            )
            return
        env = dict(os.environ)
        env["DNS_SERVER_ADMIN_PASSWORD"] = self.admin_bootstrap_password()
        log_path = self.state_dir / "technitium.log"
        log_fh = log_path.open("ab", buffering=0)
        self.daemon_pid = subprocess.Popen(
            ["dotnet", "/opt/technitium/dns/DnsServerApp.dll", "/etc/dns"],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        ).pid
        self._daemon_log_path = log_path
        log.info("technitium_dns_server_started", pid=self.daemon_pid, log_path=str(log_path))

    def daemon_running(self) -> bool:
        if self.daemon_pid is None:
            found = find_running_daemon("dotnet")
            if found is None:
                return False
            self.daemon_pid = found
            return True
        try:
            os.kill(self.daemon_pid, 0)
        except OSError:
            return False
        return not is_zombie(str(self.daemon_pid))


__all__ = ["TechnitiumDriver"]

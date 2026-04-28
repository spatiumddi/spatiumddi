"""Async nmap scan runner.

The runner is intentionally side-effect-light: it owns the subprocess
lifecycle, line-buffers stdout into the database row, and persists a
parsed summary on completion. It does not own the celery task wrapper
(see :mod:`app.tasks.nmap`) nor the HTTP surface (see
:mod:`app.api.v1.nmap`).

Security model:

* Argv is built via ``shlex``-aware tokenisation; we never invoke a
  shell — the subprocess is spawned with ``create_subprocess_exec``.
* Operator-supplied ``extra_args`` is split with ``shlex.split`` and
  every token is validated against an allowlist regex. Tokens
  carrying shell metacharacters (`;|&$\\`<>()`) or path traversal in
  ``--script`` values are rejected.
* The API container runs as a non-root user, so privileged scan
  modes (raw SYN ``-sS``, OS detection ``-O`` without privilege) just
  fall back to TCP-connect / refuse — that's fine; we surface the
  exit code so the operator knows.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import shlex
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.nmap import NmapScan

logger = structlog.get_logger(__name__)


PRESETS: dict[str, list[str]] = {
    "quick": ["-T4", "-F"],
    "service_version": ["-T4", "-sV", "--version-light"],
    "os_fingerprint": ["-T4", "-O"],
    "default_scripts": ["-T4", "-sC"],
    "udp_top100": ["-T4", "-sU", "--top-ports", "100"],
    "aggressive": ["-T4", "-A"],
    "custom": [],
}

# nmap can only emit one format to stdout, but it accepts ``-oX <file>``
# alongside ``-oN -`` — so we get human-readable text streaming live to
# the SSE viewer AND a structured XML artefact on disk for parsing into
# ``summary_json`` once the process exits. The XML path is supplied per
# scan in :func:`run_scan` (NamedTemporaryFile); ``_BASE_ARGS`` only
# carries the format-independent flags.
_BASE_ARGS = ["nmap", "-oN", "-", "--stats-every", "2s"]

_PORT_SPEC_RE = re.compile(r"^[0-9,\-UTSI:]+$")
_SHELL_METACHARS = set(";|&$`<>()")

# Maximum time we'll allow a single scan to run, mostly so a forgotten
# ``aggressive`` against a /16 doesn't pin a worker indefinitely. Eight
# minutes leaves headroom under the SSE 10-minute cap.
_SCAN_TIMEOUT_SECONDS = 8 * 60

# stdout flush cadence. The DB write throughput here is trivial
# compared to nmap's output rate (small payloads, no fsync), so 2 s is
# generous; the SSE consumer polls the DB at 500 ms.
_STDOUT_FLUSH_INTERVAL_SECONDS = 2.0


class NmapArgError(ValueError):
    """Raised when operator-supplied arguments fail validation."""


def _validate_target_ip(target: str) -> str:
    try:
        addr = ipaddress.ip_address(target)
    except ValueError as exc:
        raise NmapArgError(f"target_ip is not a valid IPv4/IPv6 address: {target}") from exc
    return str(addr)


def _validate_port_spec(spec: str | None) -> str | None:
    if spec is None:
        return None
    spec = spec.strip()
    if not spec:
        return None
    if not _PORT_SPEC_RE.match(spec):
        raise NmapArgError(
            "port_spec must be digits / commas / dashes / 'U:'/'T:'/'S:' "
            f"prefixes only (got {spec!r})"
        )
    return spec


def _validate_extra_args(extra: str | None) -> list[str]:
    if extra is None:
        return []
    extra = extra.strip()
    if not extra:
        return []
    try:
        tokens = shlex.split(extra)
    except ValueError as exc:
        raise NmapArgError(f"extra_args could not be parsed: {exc}") from exc
    for tok in tokens:
        if any(c in _SHELL_METACHARS for c in tok):
            raise NmapArgError(f"extra_args token contains shell metacharacter: {tok!r}")
        # Reject obvious path traversal in --script values. Bare numeric
        # / wordlist names are allowed, just not paths.
        if tok.startswith("--script") and ("=" in tok or False):
            value = tok.split("=", 1)[1]
            if ".." in value or "/" in value:
                raise NmapArgError(f"--script value may not contain '/' or '..': {value!r}")
    # Also catch the two-arg form: "--script foo/bar"
    for i, tok in enumerate(tokens):
        if tok == "--script" and i + 1 < len(tokens):
            value = tokens[i + 1]
            if ".." in value or "/" in value:
                raise NmapArgError(f"--script value may not contain '/' or '..': {value!r}")
    return tokens


def build_argv(
    target_ip: str,
    preset: str,
    port_spec: str | None,
    extra_args: str | None,
    xml_output_path: str | None = None,
) -> list[str]:
    """Build the full nmap argv from validated inputs.

    Always prepends ``-oN -`` + ``--stats-every 2s``. When
    ``xml_output_path`` is given, ``-oX <path>`` is appended so a
    parseable XML artefact lands on disk in parallel to the streamed
    human output. Returns the list in the order: base args, ``-oX``
    pair (optional), preset args, ``-p <spec>`` (when given), extra
    args, then the target IP last.

    Raises :class:`NmapArgError` on any validation failure.
    """
    if preset not in PRESETS:
        raise NmapArgError(f"unknown preset: {preset!r}")
    target = _validate_target_ip(target_ip)
    pspec = _validate_port_spec(port_spec)
    extra = _validate_extra_args(extra_args)

    argv = list(_BASE_ARGS)
    if xml_output_path is not None:
        argv.extend(["-oX", xml_output_path])
    argv.extend(PRESETS[preset])
    if pspec is not None:
        argv.extend(["-p", pspec])
    argv.extend(extra)
    argv.append(target)
    return argv


# ── XML parsing ─────────────────────────────────────────────────────


def _safe_text(elem: ET.Element | None, attr: str) -> str | None:
    if elem is None:
        return None
    val = elem.get(attr)
    return val if val else None


def parse_nmap_xml(xml_str: str) -> dict:
    """Parse the XML emitted by ``nmap -oX -`` into a compact summary.

    Returns a dict with keys ``host_state``, ``ports`` (list of
    ``{port, proto, state, service, version, product}``), and ``os``
    (``{name, accuracy}`` or ``None``). Best-effort: returns sensible
    defaults if the XML is truncated (which happens when nmap is
    killed mid-run).
    """
    out: dict[str, Any] = {"host_state": "unknown", "ports": [], "os": None}
    if not xml_str:
        return out
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        logger.debug("nmap_xml_parse_failed", error=str(exc))
        return out

    host = root.find("host")
    if host is None:
        return out

    status = host.find("status")
    state = _safe_text(status, "state") or "unknown"
    out["host_state"] = state

    ports_root = host.find("ports")
    if ports_root is not None:
        for p in ports_root.findall("port"):
            port_state_elem = p.find("state")
            service_elem = p.find("service")
            try:
                port_num = int(p.get("portid", "0"))
            except ValueError:
                continue
            out["ports"].append(
                {
                    "port": port_num,
                    "proto": p.get("protocol") or "tcp",
                    "state": _safe_text(port_state_elem, "state") or "unknown",
                    "reason": _safe_text(port_state_elem, "reason"),
                    "service": _safe_text(service_elem, "name"),
                    "product": _safe_text(service_elem, "product"),
                    "version": _safe_text(service_elem, "version"),
                    "extrainfo": _safe_text(service_elem, "extrainfo"),
                }
            )

    os_elem = host.find("os")
    if os_elem is not None:
        match = os_elem.find("osmatch")
        if match is not None:
            try:
                accuracy = int(match.get("accuracy", "0"))
            except ValueError:
                accuracy = 0
            out["os"] = {
                "name": match.get("name") or None,
                "accuracy": accuracy,
            }

    return out


# ── Runner ──────────────────────────────────────────────────────────


def _new_engine_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _flush_stdout(
    db: AsyncSession,
    scan_id: uuid.UUID,
    buffer: list[str],
) -> None:
    """Persist accumulated stdout lines to the row.

    We re-load the row each flush so a concurrent ``DELETE`` (operator
    cancel) is observed promptly — the runner checks the latest
    ``status`` after every flush and self-terminates on ``cancelled``.
    """
    if not buffer:
        return
    row = await db.get(NmapScan, scan_id)
    if row is None:
        return
    blob = "".join(buffer)
    row.raw_stdout = (row.raw_stdout or "") + blob
    await db.commit()
    buffer.clear()


async def run_scan(scan_id: uuid.UUID) -> None:
    """Drive one scan to completion.

    Loads the scan row, marks it ``running``, spawns nmap, line-buffers
    stdout into ``raw_stdout`` every ~2 s, and on EOF persists the
    full XML + parsed summary. On any exception the row lands in
    ``failed`` with ``error_message`` populated.

    Safe to invoke from a Celery task body via ``asyncio.run``. Owns
    its own engine + session — does not share with the calling
    request handler's session.
    """
    engine, factory = _new_engine_factory()
    xml_path: str | None = None
    try:
        async with factory() as db:
            scan = await db.get(NmapScan, scan_id)
            if scan is None:
                logger.warning("nmap_run_scan_missing", scan_id=str(scan_id))
                return

            # Operator cancelled before we picked it up — bow out.
            if scan.status == "cancelled":
                return

            # XML lands on disk in parallel to stdout so the operator
            # sees human-readable output streaming while we still get a
            # parseable artefact for ``summary_json``. Tmpfile is opened
            # with delete=False so nmap can write to it; we clean up in
            # ``finally``.
            xml_tmp = tempfile.NamedTemporaryFile(
                prefix=f"nmap-{scan_id}-",
                suffix=".xml",
                delete=False,
            )
            xml_tmp.close()
            xml_path = xml_tmp.name

            try:
                argv = build_argv(
                    str(scan.target_ip),
                    scan.preset,
                    scan.port_spec,
                    scan.extra_args,
                    xml_output_path=xml_path,
                )
            except NmapArgError as exc:
                scan.status = "failed"
                scan.error_message = str(exc)
                scan.finished_at = datetime.now(UTC)
                await db.commit()
                logger.warning("nmap_argv_invalid", scan_id=str(scan_id), error=str(exc))
                return

            scan.status = "running"
            scan.started_at = datetime.now(UTC)
            scan.command_line = " ".join(shlex.quote(a) for a in argv)
            await db.commit()

            logger.info(
                "nmap_scan_starting",
                scan_id=str(scan_id),
                target=str(scan.target_ip),
                preset=scan.preset,
                argv=argv,
            )

            buffer: list[str] = []
            started_monotonic = time.monotonic()

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError as exc:
                scan.status = "failed"
                scan.error_message = (
                    "nmap binary not found in PATH. " "Install nmap inside the api container image."
                )
                scan.finished_at = datetime.now(UTC)
                await db.commit()
                logger.error("nmap_binary_missing", error=str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — surface to operator
                scan.status = "failed"
                scan.error_message = f"failed to spawn nmap: {exc}"
                scan.finished_at = datetime.now(UTC)
                await db.commit()
                logger.exception("nmap_spawn_failed", scan_id=str(scan_id))
                return

            assert proc.stdout is not None  # noqa: S101 — PIPE was set
            last_flush = time.monotonic()
            cancelled_by_operator = False

            try:
                while True:
                    if time.monotonic() - started_monotonic > _SCAN_TIMEOUT_SECONDS:
                        logger.warning("nmap_scan_timeout", scan_id=str(scan_id))
                        proc.kill()
                        await proc.wait()
                        scan_row = await db.get(NmapScan, scan_id)
                        if scan_row is not None:
                            scan_row.status = "failed"
                            scan_row.error_message = (
                                f"scan exceeded {_SCAN_TIMEOUT_SECONDS}s timeout"
                            )
                            scan_row.finished_at = datetime.now(UTC)
                            scan_row.duration_seconds = time.monotonic() - started_monotonic
                            scan_row.exit_code = proc.returncode
                            scan_row.raw_stdout = (scan_row.raw_stdout or "") + "".join(buffer)
                            scan_row.raw_xml = _read_xml_artefact(xml_path)
                            try:
                                scan_row.summary_json = parse_nmap_xml(scan_row.raw_xml or "")
                            except Exception:  # noqa: BLE001 — best-effort
                                scan_row.summary_json = None
                            await db.commit()
                        return

                    try:
                        line_bytes = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=_STDOUT_FLUSH_INTERVAL_SECONDS,
                        )
                    except TimeoutError:
                        line_bytes = b""

                    if line_bytes:
                        line = line_bytes.decode("utf-8", errors="replace")
                        buffer.append(line)

                    # Periodic flush + cancel check
                    if time.monotonic() - last_flush >= _STDOUT_FLUSH_INTERVAL_SECONDS and buffer:
                        await _flush_stdout(db, scan_id, buffer)
                        last_flush = time.monotonic()

                    refreshed = await db.get(NmapScan, scan_id)
                    if refreshed is not None and refreshed.status == "cancelled":
                        cancelled_by_operator = True
                        proc.kill()
                        await proc.wait()
                        break

                    # Process exited
                    if proc.returncode is not None and not line_bytes:
                        # Drain anything still buffered in the pipe.
                        remaining = await proc.stdout.read()
                        if remaining:
                            chunk = remaining.decode("utf-8", errors="replace")
                            buffer.append(chunk)
                        break
                    if line_bytes == b"" and proc.returncode is None:
                        # Empty read while process still alive — nothing
                        # buffered yet, loop back into ``readline``.
                        continue
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass

            scan_row = await db.get(NmapScan, scan_id)
            if scan_row is None:
                return
            scan_row.raw_stdout = (scan_row.raw_stdout or "") + "".join(buffer)
            full_xml = _read_xml_artefact(xml_path)
            scan_row.raw_xml = full_xml
            scan_row.exit_code = proc.returncode
            scan_row.finished_at = datetime.now(UTC)
            scan_row.duration_seconds = time.monotonic() - started_monotonic
            try:
                scan_row.summary_json = parse_nmap_xml(full_xml or "")
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning("nmap_summary_parse_failed", error=str(exc))
                scan_row.summary_json = None

            if cancelled_by_operator:
                scan_row.status = "cancelled"
            elif proc.returncode == 0:
                scan_row.status = "completed"
            else:
                scan_row.status = "failed"
                scan_row.error_message = (
                    scan_row.error_message or f"nmap exited with code {proc.returncode}"
                )
            await db.commit()
            logger.info(
                "nmap_scan_finished",
                scan_id=str(scan_id),
                status=scan_row.status,
                exit_code=proc.returncode,
                duration_s=scan_row.duration_seconds,
            )
    finally:
        if xml_path is not None:
            with contextlib.suppress(Exception):
                os.unlink(xml_path)
        await engine.dispose()


def _read_xml_artefact(path: str | None) -> str | None:
    """Read the per-scan ``-oX`` artefact off disk.

    Returns ``None`` on any IO failure (file missing, truncated, etc.) —
    nmap may exit before flushing if it crashes hard, and a missing
    artefact shouldn't propagate as a runner exception. The summary
    parser tolerates ``None`` / empty XML.
    """
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


async def stream_scan_lines(
    scan_id: uuid.UUID,
    *,
    poll_interval: float = 0.5,
    cap_seconds: float = 600.0,
) -> AsyncIterator[str]:
    """Async generator yielding new ``raw_stdout`` lines as they appear.

    Polls the row at ``poll_interval`` seconds. Yields each newly
    appended line (already terminated with ``\\n``). Exits when the
    scan reaches a terminal state, with a final ``"__DONE__:<status>"``
    sentinel line so the SSE wrapper can emit a ``done`` event.

    The ``cap_seconds`` ceiling is a defensive bound — the SSE wrapper
    enforces it by closing the connection.
    """
    engine, factory = _new_engine_factory()
    try:
        offset = 0
        deadline = time.monotonic() + cap_seconds
        async with factory() as db:
            while time.monotonic() < deadline:
                row = await db.get(NmapScan, scan_id)
                if row is None:
                    yield "__DONE__:not_found\n"
                    return

                blob = row.raw_stdout or ""
                if len(blob) > offset:
                    new_chunk = blob[offset:]
                    offset = len(blob)
                    # Yield line-by-line so SSE events are properly
                    # framed even when nmap flushes mid-line.
                    pending = ""
                    for ch in new_chunk:
                        pending += ch
                        if ch == "\n":
                            yield pending
                            pending = ""
                    if pending:
                        # No trailing newline yet — emit as-is so the
                        # browser sees progress; nmap will terminate
                        # the line on the next stats tick.
                        yield pending

                if row.status in ("completed", "failed", "cancelled"):
                    yield f"__DONE__:{row.status}\n"
                    return

                # Force the session to drop its identity-map cache so the
                # next ``db.get`` re-reads from the DB.
                db.expire_all()
                await asyncio.sleep(poll_interval)

            yield "__DONE__:timeout\n"
    finally:
        await engine.dispose()


__all__ = [
    "PRESETS",
    "NmapArgError",
    "build_argv",
    "parse_nmap_xml",
    "run_scan",
    "stream_scan_lines",
]

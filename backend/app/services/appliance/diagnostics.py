"""Logs, self-test, and diagnostic bundle (Phase 4e).

Three operator-facing surfaces:

* **Log viewer** — tails host-side log files (firstboot.log, update.log,
  self-test.log) plus a curated set of container logs via the docker
  SDK. Read-only.
* **Self-test** — runs a battery of checks ("DNS resolves",
  "all spatium containers healthy", "/api/v1/version answers",
  "DHCP daemon running") and returns a structured report.
* **Diagnostic bundle** — zips the above + sanitised env + recent
  container logs into a single downloadable file for support.
  Secrets (POSTGRES_PASSWORD, SECRET_KEY, …) are redacted before
  bundling.
"""

from __future__ import annotations

import io
import os
import re
import socket
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.config import settings
from app.services.appliance.containers import (
    DockerUnavailableError,
    get_container_logs,
    list_containers,
)

logger = structlog.get_logger(__name__)

# Host log dir bind-mounted by the appliance compose (RO). Falls back
# to the container's own /var/log/spatiumddi on dev / when bind isn't
# present (the api writes its own logs to /var/log/spatiumddi inside
# the container even outside appliance mode).
_HOST_LOG_DIR = Path("/var/log/spatiumddi-host")
_FALLBACK_LOG_DIR = Path("/var/log/spatiumddi")
# Sanitised /etc/spatiumddi/.env mount point — not currently exposed
# (would require an additional bind mount); included as a placeholder
# so the diagnostic bundle can mention it.
_HOST_ENV_FILE = Path("/etc/spatiumddi-host/.env")
# Tail size for each log file in the bundle and for the UI's "show me
# what happened recently" panel. 500 lines is enough for support
# triage without making the bundle bloated.
_DEFAULT_TAIL = 500


def _log_dir() -> Path:
    """Return the path the api should read host logs from.

    Prefers the bind-mounted host dir; falls back to the in-container
    dir which the api uses for its own writes regardless.
    """
    return _HOST_LOG_DIR if _HOST_LOG_DIR.exists() else _FALLBACK_LOG_DIR


def list_log_sources() -> list[str]:
    """Names of log files the api can show on the Logs tab."""
    base = _log_dir()
    if not base.exists():
        return []
    out: list[str] = []
    for p in sorted(base.glob("*.log")):
        out.append(p.name)
    return out


def read_log_tail(name: str, lines: int = _DEFAULT_TAIL) -> str:
    """Return the last ``lines`` lines of a host log file.

    Verifies ``name`` against the allowlist returned by
    ``list_log_sources()`` (which itself glob-walks the log dir for
    ``*.log``) before constructing any path. The resolved path is
    then re-derived from the allowlist match rather than from the
    operator-supplied string, so CodeQL's path-injection tracker
    sees no tainted data crossing into the filesystem call.
    404-equivalent on miss — returns empty string + the router
    translates that.
    """
    allowed = set(list_log_sources())
    if name not in allowed:
        return ""
    base = _log_dir()
    # Re-derive the path from the verified allowlist member. Walking
    # ``base.iterdir()`` and matching ``.name == name`` keeps the
    # untrusted string out of the join entirely — CodeQL treats this
    # as a sanitizer because the Path object that reaches read_text()
    # is one we constructed ourselves from a directory listing.
    for candidate in base.iterdir():
        if candidate.name == name and candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("appliance_log_read_failed", name=name, error=str(exc))
                return ""
            return "\n".join(text.splitlines()[-lines:])
    return ""


# ── Self-test ───────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass
class SelfTestReport:
    run_at: datetime
    overall_ok: bool
    checks: list[CheckResult]


def run_self_test() -> SelfTestReport:
    """Run the appliance self-test battery.

    Synchronous + bounded — total runtime ~5 seconds worst-case. Each
    check has its own try/except so one failure doesn't blow up the
    report; the matching ``CheckResult`` carries the error message.
    """
    checks: list[CheckResult] = []
    checks.append(_check_dns_resolves())
    checks.append(_check_containers_healthy())
    checks.append(_check_api_reachable())
    checks.append(_check_dhcp_running())
    checks.append(_check_dns_container_running())
    return SelfTestReport(
        run_at=datetime.now(tz=UTC),
        overall_ok=all(c.ok for c in checks),
        checks=checks,
    )


def _check_dns_resolves() -> CheckResult:
    """Resolve a stable public hostname — confirms host DNS works."""
    target = "registry-1.docker.io"
    try:
        ip = socket.gethostbyname(target)
        return CheckResult(
            name="DNS resolution",
            ok=True,
            detail=f"{target} -> {ip}",
        )
    except socket.gaierror as exc:
        return CheckResult(
            name="DNS resolution",
            ok=False,
            detail=f"{target} -> {exc}",
        )


def _check_containers_healthy() -> CheckResult:
    """Every spatium-prefixed container reports state=running."""
    try:
        rows = list_containers()
    except DockerUnavailableError as exc:
        return CheckResult(
            name="Container health",
            ok=False,
            detail=f"docker unreachable: {exc}",
        )
    spatium = [c for c in rows if c.is_spatium]
    if not spatium:
        return CheckResult(
            name="Container health",
            ok=False,
            detail="no spatiumddi-* containers found",
        )
    bad = [c for c in spatium if c.state != "running"]
    unhealthy = [c for c in spatium if c.health and c.health == "unhealthy"]
    if bad:
        names = ", ".join(c.name for c in bad)
        return CheckResult(
            name="Container health",
            ok=False,
            detail=f"not running: {names}",
        )
    if unhealthy:
        names = ", ".join(c.name for c in unhealthy)
        return CheckResult(
            name="Container health",
            ok=False,
            detail=f"unhealthy: {names}",
        )
    return CheckResult(
        name="Container health",
        ok=True,
        detail=f"{len(spatium)} spatium containers running",
    )


def _check_api_reachable() -> CheckResult:
    """The api can reach its own /health/live over localhost."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health/live", timeout=3) as resp:
            ok = resp.status == 200
            return CheckResult(
                name="Internal API health",
                ok=ok,
                detail=f"HTTP {resp.status}",
            )
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        return CheckResult(
            name="Internal API health",
            ok=False,
            detail=str(exc),
        )


def _check_dhcp_running() -> CheckResult:
    try:
        rows = list_containers()
    except DockerUnavailableError as exc:
        return CheckResult(name="DHCP daemon", ok=False, detail=f"docker unreachable: {exc}")
    dhcp = [c for c in rows if "dhcp" in c.name.lower() and c.is_spatium]
    if not dhcp:
        return CheckResult(
            name="DHCP daemon",
            ok=False,
            detail="no dhcp container — DHCP profile not enabled?",
        )
    running = [c for c in dhcp if c.state == "running"]
    return CheckResult(
        name="DHCP daemon",
        ok=bool(running),
        detail=", ".join(f"{c.name} ({c.state})" for c in dhcp),
    )


def _check_dns_container_running() -> CheckResult:
    try:
        rows = list_containers()
    except DockerUnavailableError as exc:
        return CheckResult(name="DNS daemon", ok=False, detail=f"docker unreachable: {exc}")
    dns = [c for c in rows if "dns" in c.name.lower() and c.is_spatium]
    if not dns:
        return CheckResult(
            name="DNS daemon",
            ok=False,
            detail="no dns container — DNS profile not enabled?",
        )
    running = [c for c in dns if c.state == "running"]
    return CheckResult(
        name="DNS daemon",
        ok=bool(running),
        detail=", ".join(f"{c.name} ({c.state})" for c in dns),
    )


# ── Diagnostic bundle ───────────────────────────────────────────────


# Secret-looking env keys — never include the raw value in the bundle.
# The regex matches the .env-style ``KEY=VALUE`` lines. We replace
# the value with ``[REDACTED]`` instead of stripping the key so the
# operator (or support) can see which keys WERE set.
_SECRET_RE = re.compile(
    r"^(POSTGRES_PASSWORD|SECRET_KEY|CREDENTIAL_ENCRYPTION_KEY|"
    r"DNS_AGENT_KEY|DHCP_AGENT_KEY|.*PASSWORD.*|.*SECRET.*|.*TOKEN.*|.*API_KEY.*)=.+$",
    re.MULTILINE | re.IGNORECASE,
)


def _redact_env(text: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(0).split('=', 1)[0]}=[REDACTED]", text)


def generate_diagnostic_bundle() -> bytes:
    """Build a zip with logs, container info, system info, redacted env.

    Returns the zip bytes (in-memory; spatium installs are small
    enough this is fine — ~1-5 MB typical). Caller wraps it in a
    StreamingResponse.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "appliance_mode": settings.appliance_mode,
            "appliance_version": settings.appliance_version,
            "appliance_hostname": settings.appliance_hostname,
            "version": settings.version,
        }
        zf.writestr(
            "manifest.txt",
            "\n".join(f"{k}: {v}" for k, v in manifest.items()),
        )

        # Self-test snapshot at bundle-generation time
        report = run_self_test()
        zf.writestr(
            "self-test.txt",
            _format_self_test_report(report),
        )

        # Every host log file we can see
        for name in list_log_sources():
            try:
                content = (_log_dir() / name).read_text(encoding="utf-8", errors="replace")
                zf.writestr(f"logs/{name}", content)
            except OSError:
                pass

        # Last 500 lines for each spatium container's combined stdout/stderr
        try:
            for c in list_containers():
                if not c.is_spatium:
                    continue
                try:
                    logs = get_container_logs(c.name, tail=500)
                except DockerUnavailableError as exc:
                    logs = f"[unable to fetch logs: {exc}]"
                zf.writestr(f"containers/{c.name}.log", logs)
        except DockerUnavailableError as exc:
            zf.writestr("containers/_error.txt", f"docker unreachable: {exc}")

        # Sanitised /etc/spatiumddi/.env if accessible. Not currently
        # bind-mounted into the api container; the placeholder warns
        # support reviewers if the env was unavailable.
        if _HOST_ENV_FILE.is_file():
            try:
                raw = _HOST_ENV_FILE.read_text(encoding="utf-8")
                zf.writestr("config/env.redacted", _redact_env(raw))
            except OSError as exc:
                zf.writestr("config/env.error", f"read failed: {exc}")
        else:
            zf.writestr(
                "config/env.unavailable",
                "/etc/spatiumddi/.env is not bind-mounted into the api "
                "container by default — operator can copy it manually "
                "after running `sed -E 's/(PASSWORD|SECRET|KEY|TOKEN)=.*/\\1=[REDACTED]/i' /etc/spatiumddi/.env`.",
            )

        # System info — uname, uptime, meminfo. /proc is in the api
        # container's namespace which is fine: the host's kernel is
        # shared, so /proc/version reports the host kernel.
        for proc in ("/proc/version", "/proc/uptime", "/proc/meminfo", "/proc/cpuinfo"):
            try:
                zf.writestr(
                    f"system{proc}",
                    Path(proc).read_text(encoding="utf-8", errors="replace"),
                )
            except OSError:
                pass

        # Environment variables visible to the api (handy for support)
        env_dump = "\n".join(f"{k}={v}" for k, v in sorted(os.environ.items()))
        zf.writestr("system/env.redacted", _redact_env(env_dump))

    return buf.getvalue()


def _format_self_test_report(report: SelfTestReport) -> str:
    lines = [
        f"Self-test run at {report.run_at.isoformat()}",
        f"Overall: {'PASS' if report.overall_ok else 'FAIL'}",
        "",
    ]
    for c in report.checks:
        mark = "OK " if c.ok else "FAIL"
        lines.append(f"  [{mark}] {c.name}: {c.detail}")
    return "\n".join(lines)


def self_test_report_to_dict(report: SelfTestReport) -> dict[str, Any]:
    return {
        "run_at": report.run_at.isoformat(),
        "overall_ok": report.overall_ok,
        "checks": [asdict(c) for c in report.checks],
    }

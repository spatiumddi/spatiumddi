"""Shared WinRM / PowerShell execution chokepoint for the Windows drivers.

The Windows DNS and DHCP drivers both shell PowerShell to a Windows host
over WinRM (pywinrm). This module is the single place that builds the
session, picks transport/TLS, enforces timeouts, and — critically —
guards the ``powershell -EncodedCommand`` size limit so a large batch
fails *loudly and early* instead of silently overflowing the CMD.EXE
command-line cap and hard-failing an entire chunk (#426).

``run_ps`` is blocking — call it via ``asyncio.to_thread``.
"""

from __future__ import annotations

import base64
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# pywinrm dispatches PowerShell as ``powershell.exe -EncodedCommand <b64>``
# wrapped by CMD.EXE (``skip_cmd_shell=False``), so the whole command line
# is bound by CMD's ~8191-char limit. The encoded command is
# ``base64(utf-16-le(script))`` ≈ 2.67× the script's character count. We
# budget conservatively, leaving headroom for the ``powershell.exe
# -EncodedCommand `` prefix + CMD quoting. Dispatchers pack batches under
# this via ``encoded_command_len``.
MAX_ENCODED_COMMAND = 7800

# Timeouts: read_timeout must be strictly greater than operation_timeout
# (pywinrm asserts this). Without them a wedged call retries the
# output-receive indefinitely and ties up a worker thread forever (#426).
_OPERATION_TIMEOUT_SEC = 120
_READ_TIMEOUT_SEC = 150


class WinRMCommandTooLong(RuntimeError):
    """The encoded PowerShell command exceeds the CMD.EXE line budget.

    Raised before dispatch so the caller can split the batch rather than
    let CMD.EXE truncate it into a cryptic parser error.
    """


def encoded_command_len(script: str) -> int:
    """Length of the base64 ``-EncodedCommand`` payload pywinrm would
    send for ``script`` (UTF-16-LE → base64). Dispatchers use this to
    pack a batch under ``MAX_ENCODED_COMMAND``."""
    return len(base64.b64encode(script.encode("utf-16-le")))


def _warn_insecure_transport(host: str, transport: str, use_tls: bool, verify_tls: bool) -> None:
    """Emit a one-time-ish WARNING when the WinRM transport is insecure,
    matching the #289 hardening on the proxmox / unifi / opnsense / ftp
    clients (which previously logged nothing about disabled TLS)."""
    if use_tls and not verify_tls:
        logger.warning(
            "winrm_tls_verification_disabled",
            host=host,
            hint=(
                "WinRM over HTTPS with certificate validation OFF "
                "(verify_tls=false) — set verify_tls and install the host "
                "cert to authenticate the server."
            ),
        )
    if not use_tls and transport == "basic":
        logger.warning(
            "winrm_cleartext_basic_auth",
            host=host,
            hint=(
                "WinRM basic auth over plain HTTP ships the password "
                "effectively in cleartext — use ntlm/kerberos or enable TLS."
            ),
        )


def run_ps(
    host: str,
    creds: dict[str, Any],
    script: str,
    *,
    op_label: str = "winrm",
) -> str:
    """Run a PowerShell script on a Windows host over WinRM.

    Blocking — call via ``asyncio.to_thread``. Returns stdout as text.
    Raises :class:`WinRMCommandTooLong` if the encoded command would
    overflow the CMD.EXE budget (split the batch and retry), or
    ``RuntimeError`` on a non-zero exit / WinRM transport error.
    """
    # Guard the size BEFORE building a session so an oversized batch is a
    # clear, catchable error rather than a truncated-command failure.
    enc_len = encoded_command_len(script)
    if enc_len > MAX_ENCODED_COMMAND:
        raise WinRMCommandTooLong(
            f"{op_label}: encoded PowerShell command is {enc_len} chars, over the "
            f"{MAX_ENCODED_COMMAND} CMD.EXE budget — split the batch into smaller chunks"
        )

    # Deferred import: keeps celery-worker startup light and means hosts
    # that only run agent-based drivers don't need the pywinrm wheel.
    import winrm  # noqa: PLC0415

    transport = creds.get("transport") or "ntlm"
    use_tls = bool(creds.get("use_tls", False))
    verify_tls = bool(creds.get("verify_tls", False))
    port = int(creds.get("winrm_port") or (5986 if use_tls else 5985))
    scheme = "https" if use_tls else "http"
    endpoint = f"{scheme}://{host}:{port}/wsman"

    _warn_insecure_transport(host, transport, use_tls, verify_tls)

    session = winrm.Session(
        endpoint,
        auth=(creds.get("username", ""), creds.get("password", "")),
        transport=transport,
        server_cert_validation="validate" if verify_tls else "ignore",
        operation_timeout_sec=_OPERATION_TIMEOUT_SEC,
        read_timeout_sec=_READ_TIMEOUT_SEC,
    )
    result = session.run_ps(script)
    stdout = (result.std_out or b"").decode("utf-8", errors="replace")
    stderr = (result.std_err or b"").decode("utf-8", errors="replace")
    if result.status_code != 0:
        raise RuntimeError(
            f"winrm exit={result.status_code}: "
            f"{stderr.strip() or stdout.strip() or '<no output>'}"
        )
    return stdout

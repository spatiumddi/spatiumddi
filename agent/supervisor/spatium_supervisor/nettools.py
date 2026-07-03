"""Supervisor-side network-tool executor (dashboard-and-remote-nettools).

The control plane lets an operator run a reachability tool *from a Fleet
appliance's vantage* instead of from the api container. The api enqueues
an already-validated job (``{tool, params}``) onto the appliance's
command queue; the supervisor's nettool poll thread (see
:mod:`spatium_supervisor.nettools_proxy`) long-polls for it, runs the
tool locally via this module, and POSTs the structured result back.

This module is the local executor for the FIVE reachability tools:

  * ``ping`` / ``traceroute`` — subprocess (iputils-ping / traceroute)
  * ``dig``                   — subprocess (bind-tools)
  * ``port-test``             — pure-Python asyncio socket probe
  * ``tls-cert``              — pure-Python ssl + cryptography inspection

The supervisor is a SEPARATE python package and cannot import
``backend.app`` — so this is a minimal, self-contained vendor of the
argv builders in ``app.services.nettools.runner`` + the socket tools in
``app.services.nettools.socket_tools``. The dicts returned here match
the backend result models field-for-field (``CommandResult`` /
``PortTestResult`` / ``TlsCertResult``) so the control plane's
``result_model.model_validate(outcome.result)`` accepts them verbatim.

Security model (mirrors the backend runner):

  * Argv is built by hand from validated inputs; subprocesses are
    spawned with ``asyncio.create_subprocess_exec`` — NEVER a shell.
  * The control plane already re-validated every field with the same
    Pydantic schema before enqueuing, but we apply defence-in-depth
    here too: the SSRF/loopback/link-local denylist (127/8, ::1,
    169.254/16, fe80::/10) on every network-reaching parameter, plus a
    leading-dash guard on argv-positional values. RFC 1918 / ULA stay
    ALLOWED — diagnosing the internal network is the whole point.
  * Every call carries a hard ``asyncio.wait_for`` timeout. A missing
    binary returns a clean ``available=False`` dict — it never crashes
    the poll loop.

``ran_from`` is left at the default ``"server"`` on the returned dicts;
the control plane stamps the real ``"appliance:<name>"`` label after it
re-parses the result (it knows the appliance's hostname, the supervisor
doesn't necessarily). Including the field keeps the dict schema-valid.

NOTE: mtr + whois are intentionally NOT executed here. mtr needs
CAP_NET_RAW and its per-vantage value is covered by ping/traceroute;
whois hits shared off-prem infra and has no per-vantage meaning. Both
stay server-only on the control plane (they're absent from the backend's
``REACHABILITY_TOOLS`` set), so a job for either never reaches this
executor — but ``execute`` rejects them defensively all the same.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import time
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

log = structlog.get_logger(__name__)


# ── validation (vendored from app.services.nettools.schemas) ─────────

# RFC 1123 / 952 hostname — same shape the backend schema accepts.
_HOSTNAME_RE: Final = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)

# dig record types we pass through — lockstep with the backend allowlist.
_VALID_DIG_TYPES: Final[frozenset[str]] = frozenset(
    {
        "A",
        "AAAA",
        "CNAME",
        "MX",
        "TXT",
        "NS",
        "SOA",
        "PTR",
        "SRV",
        "CAA",
        "TLSA",
        "DS",
        "DNSKEY",
        "NAPTR",
        "ANY",
    }
)

_DNS_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,253}$")

# SSRF denylist for socket-connecting / resolver-steering params. Block
# loopback + link-local (the latter covers the 169.254.169.254 cloud
# metadata IP). RFC 1918 / ULA are deliberately NOT blocked — the whole
# point of an appliance vantage is reaching the internal network.
_BLOCKED_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)


class NetToolArgError(ValueError):
    """Raised when a job's params fail local re-validation."""


def _validate_host(value: str) -> str:
    """Accept an IPv4/IPv6 literal or an RFC 1123 hostname. Raises
    :class:`NetToolArgError` otherwise."""
    value = value.strip()
    if not value:
        raise NetToolArgError("host is required")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if _HOSTNAME_RE.match(value):
        return value
    raise NetToolArgError(
        f"host must be a valid IPv4/IPv6 address or hostname (got {value!r})"
    )


def _is_blocked_target(value: str) -> bool:
    """True when ``value`` is an IP literal inside a blocked range. A
    hostname returns False (we can't classify it without resolving)."""
    value = value.strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return any(ip in net for net in _BLOCKED_NETWORKS)


def _assert_target_allowed(value: str) -> str:
    """Validate as a host AND reject blocked-range IP literals. Use for
    any param that opens a socket or steers a resolver."""
    host = _validate_host(value)
    if _is_blocked_target(host):
        raise NetToolArgError(
            f"target {host!r} is in a blocked range (loopback / link-local "
            "/ cloud-metadata) and cannot be reached by this tool"
        )
    return host


# Cap captured subprocess output so a pathological tool can't blow out
# the reply body. 256 KiB is far more than these tools ever emit.
_MAX_OUTPUT_BYTES: Final[int] = 256 * 1024


# ── subprocess runner core (vendored from runner._run) ───────────────


async def _run(
    tool: str,
    argv: list[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Spawn ``argv`` (no shell), capture stdout/stderr, enforce a hard
    timeout, and return a ``CommandResult``-shaped dict.

    A missing binary yields ``available=False`` with a clean message
    rather than raising — the poll loop must never crash because a tool
    isn't installed in the supervisor image.
    """
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("supervisor.nettool.binary_missing", tool=tool, binary=argv[0])
        return _command_result(
            tool=tool,
            argv=argv,
            available=False,
            error=(
                f"'{argv[0]}' is not installed in the supervisor image. "
                "This tool is unavailable on this appliance."
            ),
        )
    except OSError as exc:
        log.warning("supervisor.nettool.spawn_failed", tool=tool, error=str(exc))
        return _command_result(
            tool=tool,
            argv=argv,
            available=False,
            error=f"failed to spawn '{argv[0]}': {exc}",
        )

    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            # The process already exited between the timeout and the kill —
            # nothing left to reap.
            pass
        duration_ms = (time.perf_counter() - started) * 1000.0
        log.info("supervisor.nettool.timeout", tool=tool, timeout_s=timeout_seconds)
        return _command_result(
            tool=tool,
            argv=argv,
            available=True,
            timed_out=True,
            duration_ms=duration_ms,
            error=f"{tool} exceeded the {timeout_seconds:.0f}s timeout",
        )

    duration_ms = (time.perf_counter() - started) * 1000.0
    stdout = out_b[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = err_b[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return _command_result(
        tool=tool,
        argv=argv,
        available=True,
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
    )


def _command_result(
    *,
    tool: str,
    argv: list[str],
    available: bool,
    exit_code: int | None = None,
    timed_out: bool = False,
    duration_ms: float | None = None,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """Build a dict matching the backend ``CommandResult`` model exactly.

    ``ran_from`` stays ``"server"`` — the control plane overwrites it
    with ``"appliance:<name>"`` after re-parsing (it owns the hostname).
    """
    return {
        "tool": tool,
        "argv": argv,
        "available": available,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "ran_from": "server",
    }


# ── ping ─────────────────────────────────────────────────────────────


def _build_ping_argv(host: str, *, count: int = 4) -> list[str]:
    target = _validate_host(host)
    if not 1 <= count <= 10:
        raise NetToolArgError("count must be between 1 and 10")
    # -n numeric (no reverse DNS — deterministic + fast), -c bounded
    # packets, -w overall deadline. iputils flags (NOT busybox ping):
    # busybox ping lacks -n and treats -w differently, so the image
    # must ship real iputils-ping.
    return ["ping", "-n", "-c", str(count), "-w", "15", target]


async def _run_ping(params: dict[str, Any]) -> dict[str, Any]:
    host = str(params.get("host", ""))
    argv = _build_ping_argv(host)
    return await _run("ping", argv, timeout_seconds=20.0)


# ── traceroute ───────────────────────────────────────────────────────


def _build_traceroute_argv(host: str, *, max_hops: int = 20) -> list[str]:
    target = _validate_host(host)
    if not 1 <= max_hops <= 30:
        raise NetToolArgError("max_hops must be between 1 and 30")
    # -n numeric, -m bounded hops, -w 2s per-probe wait, -q 1 single
    # probe per hop to keep wall-clock down.
    return ["traceroute", "-n", "-m", str(max_hops), "-w", "2", "-q", "1", target]


async def _run_traceroute(params: dict[str, Any]) -> dict[str, Any]:
    host = str(params.get("host", ""))
    argv = _build_traceroute_argv(host)
    return await _run("traceroute", argv, timeout_seconds=30.0)


# ── dig ──────────────────────────────────────────────────────────────


def _build_dig_argv(
    name: str, record_type: str, server: str | None = None
) -> list[str]:
    name = name.strip()
    # dig has no ``--`` end-of-options terminator, so a name / @server
    # beginning with '-' would be parsed as a flag. Reject leading-dash
    # unconditionally — this executor is a re-validation point.
    if name.startswith("-"):
        raise NetToolArgError(f"name may not start with '-': {name!r}")
    if not name or not _DNS_NAME_RE.match(name):
        raise NetToolArgError(f"name is not a valid DNS name: {name!r}")
    rtype = record_type.strip().upper()
    if rtype not in _VALID_DIG_TYPES:
        raise NetToolArgError(f"unsupported record type: {record_type!r}")
    argv = [
        "dig",
        "+nocmd",
        "+noall",
        "+answer",
        "+authority",
        "+comments",
        "+timeout=3",
        "+tries=2",
    ]
    if server is not None:
        srv = server.strip()
        if srv.startswith("-"):
            raise NetToolArgError(f"server may not start with '-': {srv!r}")
        # SSRF guard on @server — can't be aimed at loopback / metadata.
        srv = _assert_target_allowed(srv)
        argv.append(f"@{srv}")
    argv.extend([name, rtype])
    return argv


async def _run_dig(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", ""))
    record_type = str(params.get("record_type", "A"))
    server = params.get("server")
    server = str(server) if server else None
    argv = _build_dig_argv(name, record_type, server)
    return await _run("dig", argv, timeout_seconds=15.0)


# ── port-test (vendored from socket_tools.test_port) ─────────────────


def _port_test_result(
    *,
    host: str,
    port: int,
    protocol: str,
    state: str,
    rtt_ms: float | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Dict matching the backend ``PortTestResult`` model exactly."""
    return {
        "host": host,
        "port": port,
        "protocol": protocol,
        "state": state,
        "rtt_ms": rtt_ms,
        "error": error,
        "ran_from": "server",
    }


async def _test_tcp(host: str, port: int, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        fut = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        rtt_ms = (time.perf_counter() - started) * 1000.0
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 — close best-effort
            pass
        return _port_test_result(
            host=host, port=port, protocol="tcp", state="open", rtt_ms=rtt_ms
        )
    except (TimeoutError, asyncio.TimeoutError):
        return _port_test_result(
            host=host,
            port=port,
            protocol="tcp",
            state="filtered",
            error=f"no response within {timeout:.1f}s (filtered / dropped)",
        )
    except ConnectionRefusedError:
        rtt_ms = (time.perf_counter() - started) * 1000.0
        return _port_test_result(
            host=host, port=port, protocol="tcp", state="closed", rtt_ms=rtt_ms
        )
    except (OSError, socket.gaierror) as exc:
        return _port_test_result(
            host=host, port=port, protocol="tcp", state="error", error=str(exc)
        )


async def _test_udp(host: str, port: int, timeout: float) -> dict[str, Any]:
    """UDP probe — send an empty datagram, wait for an ICMP
    port-unreachable. No reply = ``open|filtered`` (the inherent UDP
    ambiguity); ECONNREFUSED = ``closed``."""
    loop = asyncio.get_running_loop()
    started = time.perf_counter()

    class _Proto(asyncio.DatagramProtocol):
        def __init__(self) -> None:
            self.error: Exception | None = None

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            transport.sendto(b"")  # type: ignore[attr-defined]

        def error_received(self, exc: Exception) -> None:
            self.error = exc

    try:
        transport, proto = await asyncio.wait_for(
            loop.create_datagram_endpoint(_Proto, remote_addr=(host, port)),
            timeout=timeout,
        )
    except (OSError, socket.gaierror, TimeoutError, asyncio.TimeoutError) as exc:
        return _port_test_result(
            host=host, port=port, protocol="udp", state="error", error=str(exc)
        )
    try:
        await asyncio.sleep(min(timeout, 2.0))
        if isinstance(proto.error, ConnectionRefusedError):
            rtt_ms = (time.perf_counter() - started) * 1000.0
            return _port_test_result(
                host=host, port=port, protocol="udp", state="closed", rtt_ms=rtt_ms
            )
        return _port_test_result(
            host=host,
            port=port,
            protocol="udp",
            state="open|filtered",
            error="no ICMP unreachable — UDP cannot distinguish open from filtered",
        )
    finally:
        transport.close()


async def _run_port_test(params: dict[str, Any]) -> dict[str, Any]:
    raw_host = str(params.get("host", ""))
    port = int(params.get("port", 0))
    protocol = str(params.get("protocol", "tcp")).lower()
    timeout = float(params.get("timeout_seconds", 5.0))
    if protocol not in {"tcp", "udp"}:
        raise NetToolArgError("protocol must be 'tcp' or 'udp'")
    if not 1 <= port <= 65535:
        raise NetToolArgError("port must be between 1 and 65535")
    # Defence in depth — block loopback / link-local / metadata literals.
    host = _validate_host(raw_host)
    if _is_blocked_target(host):
        return _port_test_result(
            host=host,
            port=port,
            protocol=protocol,
            state="error",
            error=(
                f"target {host!r} is in a blocked range (loopback / "
                "link-local / cloud-metadata) and cannot be reached"
            ),
        )
    if protocol == "udp":
        return await _test_udp(host, port, timeout)
    return await _test_tcp(host, port, timeout)


# ── tls-cert (vendored from socket_tools.inspect_tls_cert) ───────────


def _tls_cert_result(
    *,
    host: str,
    port: int,
    server_name: str | None,
    ok: bool,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dict matching the backend ``TlsCertResult`` model exactly. The
    parsed-cert fields default to the model's defaults; ``extra`` fills
    them on the success path."""
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "server_name": server_name,
        "ok": ok,
        "subject": None,
        "issuer": None,
        "san": [],
        "not_before": None,
        "not_after": None,
        "days_remaining": None,
        "expired": False,
        "self_signed": False,
        "hostname_matches": None,
        "serial": None,
        "signature_algorithm": None,
        "error": error,
        "ran_from": "server",
    }
    if extra:
        result.update(extra)
    return result


def _rfc4514(name: x509.Name) -> str:
    try:
        return name.rfc4514_string()
    except Exception:  # noqa: BLE001 — exotic name encodings
        return str(name)


def _common_names(name: x509.Name) -> list[str]:
    return [
        a.value
        for a in name.get_attributes_for_oid(NameOID.COMMON_NAME)
        if isinstance(a.value, str)
    ]


def _san_dns_names(cert: x509.Certificate) -> list[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        san = ext.value
        names = list(san.get_values_for_type(x509.DNSName))  # type: ignore[attr-defined]
        names += [
            str(ip)
            for ip in san.get_values_for_type(x509.IPAddress)  # type: ignore[attr-defined]
        ]
        return names
    except x509.ExtensionNotFound:
        return []


def _hostname_matches(server_name: str, san: list[str], cn: list[str]) -> bool:
    candidates = san or cn
    sn = server_name.lower().rstrip(".")
    for c in candidates:
        c = c.lower().rstrip(".")
        if c == sn:
            return True
        if c.startswith("*."):
            suffix = c[1:]  # ".example.com"
            host_parts = sn.split(".", 1)
            if len(host_parts) == 2 and "." + host_parts[1] == suffix:
                return True
    return False


def _parse_cert(der: bytes, server_name: str) -> dict[str, Any]:
    cert = x509.load_der_x509_certificate(der)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    now = datetime.now(UTC)
    days_remaining = (not_after - now).days
    san = _san_dns_names(cert)
    cn = _common_names(cert.subject)
    self_signed = _rfc4514(cert.subject) == _rfc4514(cert.issuer)
    try:
        sig_alg = (
            cert.signature_hash_algorithm.name
            if cert.signature_hash_algorithm
            else None
        )
    except Exception:  # noqa: BLE001
        sig_alg = None
    return {
        "subject": _rfc4514(cert.subject),
        "issuer": _rfc4514(cert.issuer),
        "san": san,
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "days_remaining": days_remaining,
        "expired": now > not_after or now < not_before,
        "self_signed": self_signed,
        "hostname_matches": _hostname_matches(server_name, san, cn),
        "serial": format(cert.serial_number, "x"),
        "signature_algorithm": sig_alg,
    }


async def _run_tls_cert(params: dict[str, Any]) -> dict[str, Any]:
    raw_host = str(params.get("host", ""))
    port = int(params.get("port", 443))
    server_name = params.get("server_name")
    server_name = str(server_name) if server_name else None
    timeout = float(params.get("timeout_seconds", 8.0))
    if not 1 <= port <= 65535:
        raise NetToolArgError("port must be between 1 and 65535")
    host = _validate_host(raw_host)
    sni = server_name or host
    if _is_blocked_target(host):
        return _tls_cert_result(
            host=host,
            port=port,
            server_name=sni,
            ok=False,
            error=(
                f"target {host!r} is in a blocked range (loopback / "
                "link-local / cloud-metadata) and cannot be reached"
            ),
        )

    # Verification disabled — we inspect whatever the server presents
    # (expired / self-signed / mismatched). hostname_matches / expired /
    # self_signed are reported as data, not enforced.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        fut = asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni)
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        return _tls_cert_result(
            host=host,
            port=port,
            server_name=sni,
            ok=False,
            error=f"TLS handshake timed out after {timeout:.1f}s",
        )
    except (ssl.SSLError, OSError, socket.gaierror) as exc:
        return _tls_cert_result(
            host=host,
            port=port,
            server_name=sni,
            ok=False,
            error=f"TLS connection failed: {exc}",
        )

    try:
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
        if not der:
            return _tls_cert_result(
                host=host,
                port=port,
                server_name=sni,
                ok=False,
                error="server presented no certificate",
            )
        parsed = _parse_cert(der, sni)
        return _tls_cert_result(
            host=host, port=port, server_name=sni, ok=True, extra=parsed
        )
    except Exception as exc:  # noqa: BLE001 — surface parse failures cleanly
        log.warning("supervisor.nettool.tls_parse_failed", host=host, error=str(exc))
        return _tls_cert_result(
            host=host,
            port=port,
            server_name=sni,
            ok=False,
            error=f"failed to parse certificate: {exc}",
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ── firewall logs (#404) ─────────────────────────────────────────────


async def _run_firewall_logs(params: dict[str, Any]) -> dict[str, Any]:
    """Serve buffered nftables drop-log lines from the kmsg reader.

    Not a reachability probe — this reads the local kmsg ring buffer
    (#404) for lines our firewall renderer tagged ``spatium-fw: ``.
    ``since_seq`` is the incremental cursor; the result carries the new
    cursor so the UI can poll for just-arrived lines.
    """
    from . import kmsg_reader  # noqa: PLC0415

    try:
        since_seq = int(params.get("since_seq", 0) or 0)
    except (TypeError, ValueError):
        since_seq = 0
    try:
        limit = int(params.get("limit", 200) or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))
    lines, cursor = kmsg_reader.get_since(since_seq, limit)
    return {
        "available": kmsg_reader.is_available(),
        "lines": [{"seq": s, "ts_us": t, "text": txt} for (s, t, txt) in lines],
        "cursor": cursor,
    }


# ── wake-on-lan ──────────────────────────────────────────────────────

_WOL_HEX12_RE: Final = re.compile(r"^[0-9A-Fa-f]{12}$")


def _wol_normalize_mac(mac: str) -> str:
    stripped = re.sub(r"[:.\-]", "", mac.strip())
    if not _WOL_HEX12_RE.match(stripped):
        raise NetToolArgError(f"not a valid MAC address: {mac!r}")
    low = stripped.lower()
    return ":".join(low[i : i + 2] for i in range(0, 12, 2))


def _send_magic_packet(mac: str, broadcast: str, port: int) -> None:
    mac_bytes = bytes.fromhex(_wol_normalize_mac(mac).replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16  # AMD Magic Packet, 102 bytes
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))


async def _run_wol(params: dict[str, Any]) -> dict[str, Any]:
    """Send a Wake-on-LAN magic packet from the supervisor's local vantage
    (#533) so it originates on the target's broadcast domain. Re-validates
    MAC + broadcast — the control plane already did; this executor is a
    re-validation point. Returns the backend ``WolResult`` shape."""
    mac = _wol_normalize_mac(str(params.get("mac", "")))
    raw_bcast = str(params.get("broadcast", "")).strip()
    try:
        broadcast = str(ipaddress.IPv4Address(raw_bcast))
    except ipaddress.AddressValueError as exc:
        raise NetToolArgError(
            f"broadcast must be an IPv4 address: {raw_bcast!r}"
        ) from exc
    # Defence-in-depth SSRF denylist (same as every other network-reaching
    # param here) — refuse loopback / link-local / metadata even if the
    # control plane somehow enqueued one.
    if _is_blocked_target(broadcast):
        raise NetToolArgError(
            f"broadcast {broadcast!r} is in a blocked range and cannot be targeted"
        )
    try:
        port = int(params.get("port", 9))
    except (TypeError, ValueError) as exc:
        raise NetToolArgError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise NetToolArgError("port must be between 1 and 65535")
    await asyncio.to_thread(_send_magic_packet, mac, broadcast, port)
    return {
        "mac": mac,
        "broadcast": broadcast,
        "port": port,
        "sent": True,
        "ran_from": "server",
    }


# ── dispatch ─────────────────────────────────────────────────────────

# The reachability tools this executor knows how to run. Mirrors the
# backend's ``agent_cmd.REACHABILITY_TOOLS`` set — a job for any tool
# outside it is rejected (the control plane never enqueues one, but we
# guard defensively so a malformed command can't crash the loop).
_RUNNERS: Final[dict[str, Any]] = {
    "ping": _run_ping,
    "traceroute": _run_traceroute,
    "dig": _run_dig,
    "port-test": _run_port_test,
    "tls-cert": _run_tls_cert,
    # #404 — appliance-diagnostic tool (not a reachability probe), dispatched
    # the same way; the backend allows it via an explicit allow-set.
    "firewall_logs": _run_firewall_logs,
    # #533 — Wake-on-LAN. Not a reachability probe; dispatched from the IP
    # detail modal so the packet originates on the target's segment.
    "wol": _run_wol,
}

# True reachability probes only — keep firewall_logs out of this so the set
# keeps its meaning (the backend gates appliance dispatch on its own set).
REACHABILITY_TOOLS: Final[frozenset[str]] = frozenset(
    {"ping", "traceroute", "dig", "port-test", "tls-cert"}
)


async def execute(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run ``tool`` against the supervisor's local vantage and return a
    result dict matching the backend model for that tool.

    Returns ``{"result": <dict>}`` on success or ``{"error": <str>}``
    when the tool is unknown or local re-validation failed — exactly the
    two-arm shape the nettool reply endpoint expects (``result`` xor
    ``error``). This function never raises: any unexpected error is
    caught and surfaced as ``{"error": ...}`` so the caller's poll loop
    keeps running.
    """
    runner = _RUNNERS.get(tool)
    if runner is None:
        return {"error": f"unknown or non-reachability tool: {tool!r}"}
    if not isinstance(params, dict):
        return {"error": "params must be an object"}
    try:
        result = await runner(params)
        return {"result": result}
    except NetToolArgError as exc:
        # Local re-validation failure — the control plane already
        # validated, so this is defence in depth; surface a clean error.
        log.warning("supervisor.nettool.arg_error", tool=tool, error=str(exc))
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never crash the poll loop
        log.warning("supervisor.nettool.execute_crashed", tool=tool, error=str(exc))
        return {"error": f"{tool} failed: {exc}"}


__all__ = ["REACHABILITY_TOOLS", "NetToolArgError", "execute"]

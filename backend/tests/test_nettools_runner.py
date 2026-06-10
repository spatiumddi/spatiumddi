"""Hermetic tests for the network-tools subprocess runner (#58).

Every test mocks ``asyncio.create_subprocess_exec`` — no real binary is
invoked and no packets leave the box. We assert:

* argv builders reject shell-metachar / injection in targets + options;
* the timeout path returns ``timed_out`` not an exception;
* a missing binary returns ``available=False`` with a clean error, never
  a 500.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.nettools import runner
from app.services.nettools.runner import (
    NetToolArgError,
    build_dig_argv,
    build_mtr_argv,
    build_ping_argv,
    build_traceroute_argv,
    build_whois_argv,
)

# ── argv validation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "1.1.1.1; rm -rf /",
        "$(reboot)",
        "`id`",
        "host && curl evil",
        "a|b",
        "8.8.8.8 -oN /etc/passwd",  # space → not a valid single host
        "../etc/passwd",
        "",
    ],
)
def test_ping_argv_rejects_injection(bad: str) -> None:
    with pytest.raises((NetToolArgError, ValueError)):
        build_ping_argv(bad)


def test_ping_argv_accepts_ip_and_hostname() -> None:
    assert build_ping_argv("1.1.1.1") == ["ping", "-n", "-c", "4", "-w", "15", "1.1.1.1"]
    argv = build_ping_argv("router1.lan")
    assert argv[-1] == "router1.lan"
    # never a shell — argv is a flat list, target is the last element
    assert "ping" == argv[0]


def test_traceroute_argv_bounds_hops() -> None:
    argv = build_traceroute_argv("8.8.8.8", max_hops=15)
    assert "-m" in argv and "15" in argv
    with pytest.raises(NetToolArgError):
        build_traceroute_argv("8.8.8.8", max_hops=99)


def test_mtr_argv_report_mode_and_bounds() -> None:
    argv = build_mtr_argv("8.8.8.8", cycles=3)
    assert "--report" in argv and "-c" in argv and "3" in argv
    with pytest.raises(NetToolArgError):
        build_mtr_argv("8.8.8.8", cycles=0)


def test_dig_argv_rejects_bad_type_and_name() -> None:
    assert build_dig_argv("example.com", "A")[-2:] == ["example.com", "A"]
    # server is prefixed with @
    argv = build_dig_argv("example.com", "MX", server="9.9.9.9")
    assert "@9.9.9.9" in argv
    with pytest.raises(NetToolArgError):
        build_dig_argv("example.com", "EVIL")
    with pytest.raises(NetToolArgError):
        build_dig_argv("bad name with spaces", "A")


def test_whois_argv_terminates_options() -> None:
    # ``--`` guard means a query starting with '-' can't be read as a flag
    argv = build_whois_argv("13335")
    assert argv == ["whois", "--", "13335"]
    assert build_whois_argv("AS13335")[-1] == "AS13335"
    assert build_whois_argv("example.com")[-1] == "example.com"
    with pytest.raises(ValueError):
        build_whois_argv("evil; rm -rf /")


# ── _run behaviour (subprocess fully mocked) ────────────────────────


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


async def test_run_captures_output() -> None:
    fake = _fake_proc(stdout=b"PING 1.1.1.1 ok\n", returncode=0)
    with patch.object(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        res = await runner.run_ping("1.1.1.1")
    assert res.available is True
    assert res.exit_code == 0
    assert "PING 1.1.1.1 ok" in res.stdout
    assert res.timed_out is False


async def test_run_binary_missing_is_clean_error_not_500() -> None:
    with patch.object(
        runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError()),
    ):
        res = await runner.run_traceroute("8.8.8.8")
    # No exception — a structured result with available=False
    assert res.available is False
    assert res.exit_code is None
    assert res.error is not None
    assert "not installed" in res.error


async def test_run_timeout_path() -> None:
    fake = _fake_proc()

    async def _never(*_a, **_k):  # noqa: ANN002, ANN003
        await asyncio.sleep(10)

    fake.communicate = _never
    with patch.object(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        # Patch wait_for to raise TimeoutError immediately rather than
        # actually sleeping — keeps the test fast + hermetic.
        with patch.object(runner.asyncio, "wait_for", AsyncMock(side_effect=TimeoutError())):
            res = await runner.run_mtr("8.8.8.8")
    assert res.available is True
    assert res.timed_out is True
    assert res.error is not None and "timeout" in res.error.lower()
    fake.kill.assert_called_once()

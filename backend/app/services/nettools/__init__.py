"""Built-in network tools — stateless, synchronous server-perspective
utilities (issue #58).

Two halves:

* :mod:`app.services.nettools.runner` — subprocess-backed tools
  (ping / traceroute / mtr / dig / whois). Argv is allowlist-validated
  and spawned via ``create_subprocess_exec`` (never a shell), with a
  hard per-call asyncio timeout. A missing binary returns a clean
  structured "not available" result rather than a 500.
* :mod:`app.services.nettools.socket_tools` — pure-Python tools
  (port-test, TLS cert inspection) with no subprocess.

Everything here is server-perspective only. Agent-perspective dispatch
is deferred (#58 follow-up).
"""

from __future__ import annotations

from app.services.nettools.runner import (
    NetToolArgError,
    run_dig,
    run_mtr,
    run_ping,
    run_traceroute,
    run_whois,
)
from app.services.nettools.socket_tools import inspect_tls_cert, test_port

__all__ = [
    "NetToolArgError",
    "inspect_tls_cert",
    "run_dig",
    "run_mtr",
    "run_ping",
    "run_traceroute",
    "run_whois",
    "test_port",
]

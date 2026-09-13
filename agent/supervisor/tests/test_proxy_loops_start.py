"""Every long-poll proxy thread must reach its first poll (#1072).

The supervisor starts four of these from ``__main__`` — k8s, nettool,
storage, pcap — each a daemon thread whose body is ``with httpx.Client(...)
as client: while True: try: _once(...) except Exception: ...``. The guard
is INSIDE the ``with``, so anything that goes wrong while building the
client ends the thread with nothing but a traceback in the pod log; the
process, the heartbeat and the Fleet page all carry on looking healthy.
That is how the storage loop shipped dead on every appliance (#1072): it
read ``cfg.verify_tls``, which ``SupervisorConfig`` never had, and none of
its tests entered the loop.

So each loop is driven here with a config built the way
``SupervisorConfig.from_env`` builds it — the real frozen dataclass, no
attribute stand-ins — and its poll stubbed to end the loop after one pass.
A loop that cannot get that far fails here instead of on the fleet.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from spatium_supervisor import k8s_proxy, nettools_proxy, pcap_proxy, storage_proxy
from spatium_supervisor.config import SupervisorConfig
from spatium_supervisor.identity import load_or_generate


class _FirstPassDone(BaseException):
    """Ends the forever-loop. A ``BaseException`` so the loop's
    ``except Exception`` guard cannot swallow it and hang the test."""


def _bare_config(state_dir: Path) -> SupervisorConfig:
    """Exactly the seven fields ``from_env`` populates, nothing stapled on."""
    return SupervisorConfig(
        control_plane_url="https://control-plane.example",
        hostname="qa-node",
        state_dir=state_dir,
        bootstrap_pairing_code="",
        heartbeat_interval_seconds=30,
        k8s_proxy_enabled=False,
        in_pod_firewall_enabled=True,
    )


_LOOPS = [
    pytest.param(k8s_proxy, "proxy_loop_forever", "_proxy_once", id="k8s"),
    pytest.param(nettools_proxy, "nettool_loop_forever", "_nettool_once", id="nettool"),
    pytest.param(storage_proxy, "storage_loop_forever", "_storage_once", id="storage"),
    pytest.param(pcap_proxy, "pcap_loop_forever", "_pcap_once", id="pcap"),
]


@pytest.mark.parametrize(("module", "loop_name", "once_name"), _LOOPS)
def test_the_loop_reaches_its_first_poll(
    module: ModuleType,
    loop_name: str,
    once_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def _once(*args: object) -> None:
        calls.append(args)
        raise _FirstPassDone

    monkeypatch.setattr(module, once_name, _once)
    identity, _ = load_or_generate(tmp_path)
    with pytest.raises(_FirstPassDone):
        getattr(module, loop_name)(_bare_config(tmp_path), identity)
    assert len(calls) == 1

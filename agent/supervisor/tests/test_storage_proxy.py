"""Supervisor → host-runner storage bridge (#999 Part B).

``run_action`` is the whole bridge: write a request the host runner's
``.path`` unit fires on, wait for its result, clean up. These drive it
against a real temporary directory rather than mocking the filesystem,
because the failure modes are all about files existing or not.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from spatium_supervisor import storage_proxy as sp
from spatium_supervisor.config import SupervisorConfig
from spatium_supervisor.identity import Identity, load_or_generate


@pytest.fixture
def reqdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "release-state" / "storage"
    monkeypatch.setattr(sp, "_request_dir", lambda: d)
    monkeypatch.setattr(sp, "_RESULT_TIMEOUT_S", 3.0)
    monkeypatch.setattr(sp, "_RESULT_POLL_S", 0.05)
    return d


def _answer(reqdir: Path, payload: dict) -> threading.Thread:
    """Stand in for the host runner: wait for a request, write a result."""

    def run() -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            reqs = list(reqdir.glob("*.request.json"))
            if reqs:
                rid = reqs[0].name[: -len(".request.json")]
                (reqdir / f"{rid}.result.json").write_text(json.dumps(payload))
                return
            time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_the_request_is_the_structured_action(reqdir: Path) -> None:
    """Never a shell string: the host runner rebuilds the argv itself."""
    seen: dict = {}

    def run() -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            reqs = list(reqdir.glob("*.request.json"))
            if reqs:
                seen.update(json.loads(reqs[0].read_text()))
                rid = reqs[0].name[: -len(".request.json")]
                (reqdir / f"{rid}.result.json").write_text(
                    json.dumps({"ok": True, "detail": "done"})
                )
                return
            time.sleep(0.02)

    threading.Thread(target=run, daemon=True).start()
    out = sp.run_action(
        {"action": "fail_member", "array": "/dev/md/root_a", "device": "/dev/sdb4"}
    )
    assert out["result"]["ok"] is True
    assert seen == {
        "action": "fail_member",
        "array": "/dev/md/root_a",
        "device": "/dev/sdb4",
    }


def test_the_result_is_relayed(reqdir: Path) -> None:
    _answer(reqdir, {"ok": False, "detail": "REFUSED: last member", "output": ""})
    out = sp.run_action({"action": "remove_member"})
    assert out["result"]["ok"] is False
    assert "REFUSED" in out["result"]["detail"]


def test_a_silent_runner_is_reported_not_left_hanging(reqdir: Path) -> None:
    """"The runner did not answer" and "the control plane could not reach
    this appliance" are different faults with different fixes. Collapsing
    them into one spinner is how an operator power-cycles a box that was
    merely resyncing."""
    out = sp.run_action({"action": "scrub_start", "array": "/dev/md/root_a"})
    assert "error" in out
    assert "did not answer" in out["error"]


def test_files_are_cleaned_up_even_on_the_timeout_path(reqdir: Path) -> None:
    """A request left behind is picked up and ACTED ON by a later firing
    of the .path unit, long after the operator gave up."""
    sp.run_action({"action": "scrub_start", "array": "/dev/md/root_a"})
    assert list(reqdir.glob("*.json")) == []


def test_files_are_cleaned_up_after_success(reqdir: Path) -> None:
    _answer(reqdir, {"ok": True, "detail": "done"})
    sp.run_action({"action": "scrub_start", "array": "/dev/md/root_a"})
    assert list(reqdir.glob("*.json")) == []


def test_an_action_with_no_name_is_refused(reqdir: Path) -> None:
    assert "error" in sp.run_action({})


def test_a_missing_host_mount_is_reported(tmp_path: Path, monkeypatch) -> None:
    """Off-appliance the release-state bind mount does not exist. That is
    a clean "unavailable here", not a crash."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    monkeypatch.setattr(sp, "_request_dir", lambda: blocker / "storage")
    out = sp.run_action({"action": "scrub_start"})
    assert "error" in out
    assert "unavailable" in out["error"]


def test_the_reply_body_carries_the_request_id() -> None:
    """StorageReplyRequest REQUIRES it and the endpoint 422s without it —
    which, with the status unchecked, meant every storage action timed
    out at the operator's end with nothing logged anywhere."""
    src = Path(sp.__file__).read_text()
    assert '"request_id": request_id' in src
    # ...and the reply status is checked, so a rejected reply is not
    # indistinguishable from an appliance that never answered.
    assert "supervisor.storage.reply_rejected" in src


# ── #1072 — the loop must actually start ─────────────────────────────────


def _bare_config(state_dir: Path) -> SupervisorConfig:
    """A ``SupervisorConfig`` with exactly the fields ``from_env`` builds.

    Deliberately the real frozen dataclass and not a namespace or a mock:
    the defect was the loop reading an attribute the real config does not
    have, and a stand-in that answers every attribute would hide exactly
    that.
    """
    return SupervisorConfig(
        control_plane_url="https://control-plane.example",
        hostname="qa-node",
        state_dir=state_dir,
        bootstrap_pairing_code="",
        heartbeat_interval_seconds=30,
        k8s_proxy_enabled=False,
        in_pod_firewall_enabled=True,
    )


class _FirstPassDone(BaseException):
    """Raised by the stubbed poll so the forever-loop returns after one pass.

    A ``BaseException`` on purpose: the loop's ``except Exception`` guard
    must not be able to swallow it, or a passing run would hang.
    """


def test_the_loop_reaches_its_first_poll_with_a_bare_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread died on its first instruction, on every appliance (#1072).

    ``storage_loop_forever`` built its client with ``verify=cfg.verify_tls``
    and ``SupervisorConfig`` has no such field. The AttributeError was raised
    one line ABOVE the ``except Exception`` guard written to keep the thread
    alive, so it left the function and ended the thread — silently, apart
    from one traceback in the pod log at boot. None of the tests above enter
    the loop; this one does, with the real client, and proves control
    reaches ``_storage_once``.
    """
    seen: list[httpx.Client] = []

    def _once(cfg: SupervisorConfig, identity: Identity, client: httpx.Client) -> None:
        seen.append(client)
        raise _FirstPassDone

    monkeypatch.setattr(sp, "_storage_once", _once)
    identity, _ = load_or_generate(tmp_path)
    with pytest.raises(_FirstPassDone):
        sp.storage_loop_forever(_bare_config(tmp_path), identity)
    assert len(seen) == 1
    assert isinstance(seen[0], httpx.Client)

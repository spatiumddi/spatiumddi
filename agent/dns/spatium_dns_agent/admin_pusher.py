"""Admin runtime-state pushers — rendered-config snapshot + rndc status.

Two operator-diagnostics pieces ship from here, both keyed off the
new ``/api/v1/dns/agents/admin/*`` endpoints:

- ``push_rendered_config(...)`` is called by the sync loop right after
  a successful structural apply. It walks the on-disk rendered tree
  (``state_dir/rendered/``) and POSTs every file's relative path +
  text content. The control plane keeps the most-recent snapshot per
  server in ``dns_server_runtime_state.rendered_files`` so operators
  can answer "is the server actually running the config we sent?"
  without SSHing in.
- ``RndcStatusPoller`` is a long-lived thread that shells out to
  ``rndc status`` once a minute, capturing stdout, and POSTs the text
  to ``/admin/rndc-status``. The Overview tab on the Server Detail
  modal renders the latest snapshot.

Both pushes are best-effort: the agent's primary loop never blocks on
them, and a transient 5xx just gets retried on the next tick.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterable

import httpx
import structlog

from .config import AgentConfig

log = structlog.get_logger(__name__)

# Skip files that the agent writes alongside the rendered tree but
# aren't meaningful for the operator (binary state, locks, etc).
_SKIP_FILE_NAMES: frozenset[str] = frozenset()
_RENDERED_DIR_NAME = "rendered"


def _cp_client(cfg: AgentConfig) -> httpx.Client:
    verify: bool | str = True
    if cfg.insecure_skip_tls_verify:
        verify = False
    elif cfg.tls_ca_path:
        verify = cfg.tls_ca_path
    return httpx.Client(base_url=cfg.control_plane_url, verify=verify, timeout=15.0)


def _walk_rendered(rendered_dir: Path) -> Iterable[tuple[str, str]]:
    """Yield ``(relpath, content)`` pairs for every text file in the tree.

    Binary files (TSIG keyrings, etc) and anything we can't decode as
    UTF-8 are skipped — the operator-facing UI only renders text.
    """
    if not rendered_dir.is_dir():
        return
    for path in sorted(rendered_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in _SKIP_FILE_NAMES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(rendered_dir).as_posix()
        yield rel, content


def push_rendered_config(cfg: AgentConfig, token: str) -> None:
    """One-shot push of every text file under ``state_dir/rendered/``."""
    rendered = cfg.state_dir / _RENDERED_DIR_NAME
    files = [{"path": rel, "content": content} for rel, content in _walk_rendered(rendered)]
    if not files:
        log.debug("rendered_config_push_skipped", reason="no_files")
        return
    try:
        with _cp_client(cfg) as c:
            resp = c.post(
                "/api/v1/dns/agents/admin/rendered-config",
                json={"files": files},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code not in (200, 204):
            log.warning("rendered_config_push_non200", status=resp.status_code)
    except httpx.HTTPError as e:
        log.warning("rendered_config_push_http_error", error=str(e))


class RndcStatusPoller:
    """Periodic ``rndc status`` capture + push.

    Cadence is 60 s with ±3 s jitter, matching the metrics poller. We
    don't push when ``rndc`` isn't on PATH (agent might be running
    against a non-BIND9 driver in the future) or when the command
    fails — the operator-facing UI will simply show "no snapshot yet"
    in that case.
    """

    def __init__(self, cfg: AgentConfig, token_ref: list[str]):
        self.cfg = cfg
        self.token_ref = token_ref
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _capture(self) -> str | None:
        if not shutil.which("rndc"):
            return None
        # Prefer the agent-owned rndc.conf the entrypoint writes — the
        # system /etc/bind/rndc.key is mode 600 inside a restricted dir
        # so the agent user can't read it directly. The conf wraps the
        # key + supplies default-server so plain `rndc status` works.
        cmd = ["rndc"]
        agent_conf = self.cfg.state_dir / "rndc.conf"
        if agent_conf.exists():
            cmd += ["-c", str(agent_conf)]
        cmd.append("status")
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("rndc_status_capture_failed", error=str(e))
            return None
        if res.returncode != 0:
            return f"rndc status exited {res.returncode}\n{res.stderr.strip()}"
        return res.stdout

    def _report(self, text: str) -> None:
        try:
            with _cp_client(self.cfg) as c:
                resp = c.post(
                    "/api/v1/dns/agents/admin/rndc-status",
                    json={"text": text},
                    headers={"Authorization": f"Bearer {self.token_ref[0]}"},
                )
            if resp.status_code not in (200, 204):
                log.warning("rndc_status_push_non200", status=resp.status_code)
        except httpx.HTTPError as e:
            log.warning("rndc_status_push_http_error", error=str(e))

    def run(self) -> None:
        while not self._stop.is_set():
            text = self._capture()
            if text is not None:
                self._report(text)
            interval = 60.0 + random.uniform(-3, 3)
            self._stop.wait(timeout=max(30.0, interval))


__all__ = ["RndcStatusPoller", "push_rendered_config"]

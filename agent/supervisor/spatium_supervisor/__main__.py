"""CLI entrypoint — ``spatium-supervisor``.

Wave A2 supervisor loop:

1. Configure logging + state-dir layout.
2. Load (or first-boot generate) the Ed25519 identity.
3. If we already have a cached ``appliance_id``, skip register and
   idle (Wave A3+ will replace idle with a real /poll loop).
4. Otherwise: if a pairing code is present in env, call
   /supervisor/register. On success persist the appliance_id + idle.
   On disabled (404) idle + retry next boot. On fatal idle forever.
5. Idle = log heartbeat every ``heartbeat_interval_seconds`` until
   SIGTERM / SIGINT.

Wave A2 still doesn't drive any service containers, render any
firewall rules, or poll the control plane for instructions — those
are Waves C / D. This is the identity + registration foundation.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import httpx
import structlog

from .config import SupervisorConfig
from .heartbeat import heartbeat_once
from .identity import (
    load_appliance_id,
    load_or_generate,
    load_session_token,
    save_appliance_id,
    save_session_token,
)
from .log import configure_logging
from .register import RegisterDisabled, RegisterFatal, register
from .state import ensure_layout


def _build_http_client(skip_tls_verify: bool) -> httpx.Client:
    """Wave A2's client doesn't yet use mTLS (cert lands in B1). Honour
    SPATIUM_INSECURE_SKIP_TLS_VERIFY=1 so dev appliances pointed at a
    self-signed control plane still register."""
    return httpx.Client(verify=not skip_tls_verify)


def _maybe_register(
    cfg: SupervisorConfig, log: structlog.stdlib.BoundLogger
) -> None:
    """Run identity generation + register-if-needed in one shot. Logs
    its own status; never raises into the caller (the main loop
    falls back to idle on any failure)."""
    identity, generated = load_or_generate(cfg.state_dir)
    if generated:
        log.info(
            "supervisor.identity.generated",
            fingerprint=identity.fingerprint,
        )
    else:
        log.info(
            "supervisor.identity.loaded",
            fingerprint=identity.fingerprint,
        )

    cached_appliance_id = load_appliance_id(cfg.state_dir)
    if cached_appliance_id is not None:
        log.info(
            "supervisor.register.cached",
            appliance_id=str(cached_appliance_id),
        )
        return

    if not cfg.control_plane_url:
        log.warning("supervisor.register.skipped", reason="no control_plane_url")
        return
    if not cfg.bootstrap_pairing_code:
        log.warning("supervisor.register.skipped", reason="no bootstrap_pairing_code")
        return

    skip_tls = os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        with _build_http_client(skip_tls_verify=skip_tls) as client:
            result = register(
                control_plane_url=cfg.control_plane_url,
                pairing_code=cfg.bootstrap_pairing_code,
                identity=identity,
                hostname=cfg.hostname,
                supervisor_version=_supervisor_version(),
                client=client,
            )
    except RegisterDisabled as exc:
        log.warning("supervisor.register.disabled", reason=str(exc))
        return
    except RegisterFatal as exc:
        log.error("supervisor.register.fatal", reason=str(exc))
        return

    import uuid

    save_appliance_id(cfg.state_dir, uuid.UUID(result.appliance_id))
    # Stash the cleartext session token alongside the appliance_id
    # so the heartbeat loop can authenticate without a fresh register
    # call across supervisor restarts. Cleared when mTLS lands.
    save_session_token(cfg.state_dir, result.session_token)
    log.info(
        "supervisor.register.persisted",
        appliance_id=result.appliance_id,
        state=result.state,
    )


def _supervisor_version() -> str:
    from . import __version__

    return __version__


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    log = structlog.get_logger()

    cfg = SupervisorConfig.from_env()
    ensure_layout(cfg.state_dir)

    log.info(
        "supervisor.start",
        phase="A2-identity-register",
        hostname=cfg.hostname,
        control_plane_url=cfg.control_plane_url or None,
        bootstrap_pairing_code_set=bool(cfg.bootstrap_pairing_code),
        state_dir=str(cfg.state_dir),
        heartbeat_interval_seconds=cfg.heartbeat_interval_seconds,
    )

    _maybe_register(cfg, log)

    stop = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        log.info("supervisor.signal", signal=signum)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # #170 Wave C1 — heartbeat loop replaces the A2 idle. Every
    # ``heartbeat_interval_seconds`` we POST appliance-host telemetry
    # to the control plane and read back the operator's desired state
    # (upgrade / reboot triggers). Only fires when register has
    # produced an appliance_id; otherwise we keep idling so a re-pair
    # from a fresh code can still land.
    skip_tls = os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    while not stop:
        appliance_id = load_appliance_id(cfg.state_dir)
        if appliance_id is not None and cfg.control_plane_url:
            session_token = load_session_token(cfg.state_dir)
            identity, _ = load_or_generate(cfg.state_dir)
            try:
                with _build_http_client(skip_tls_verify=skip_tls) as client:
                    heartbeat_once(
                        cfg=cfg,
                        appliance_id=appliance_id,
                        session_token=session_token,
                        identity=identity,
                        client=client,
                        log=log,
                    )
            except Exception as exc:  # noqa: BLE001
                # Never let a heartbeat exception kill the supervisor —
                # the loop is the supervisor's sole liveness signal.
                log.warning("supervisor.heartbeat.crashed", error=str(exc))
        else:
            log.info(
                "supervisor.heartbeat.skipped",
                reason=(
                    "no_appliance_id"
                    if appliance_id is None
                    else "no_control_plane_url"
                ),
            )
        for _ in range(cfg.heartbeat_interval_seconds):
            if stop:
                break
            time.sleep(1)

    log.info("supervisor.stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())

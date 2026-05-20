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

import dataclasses

from . import appliance_state, approval_state
from .cert_auth import clear_cert
from .config import SupervisorConfig
from .heartbeat import heartbeat_once
from .k8s_proxy import start_proxy_thread
from .identity import (
    clear_appliance_id,
    clear_session_token,
    load_appliance_id,
    load_or_generate,
    load_session_token,
    save_appliance_id,
    save_session_token,
)
from .log import configure_logging
from .register import RegisterDisabled, RegisterFatal, register
from .state import ensure_layout


def _build_http_client(
    skip_tls_verify: bool,
    *,
    log: structlog.stdlib.BoundLogger | None = None,
) -> httpx.Client:
    """Wave A2's client doesn't yet use mTLS (cert lands in B1). Honour
    SPATIUM_INSECURE_SKIP_TLS_VERIFY=1 so dev appliances pointed at a
    self-signed control plane still register.

    Issue #234 — when the opt-out is set, log a prominent WARNING on
    every build (de-duped via a function attribute so spam stays
    bounded). The pre-#234 behaviour was a silent ``verify=False``
    with no log surface, so a misset env on a production appliance
    disabled TLS verification across every heartbeat with no
    operator-visible indicator.
    """
    if skip_tls_verify and not getattr(_build_http_client, "_warned", False):
        _build_http_client._warned = True  # type: ignore[attr-defined]
        (log or structlog.get_logger(__name__)).warning(
            "supervisor.tls_verify_disabled",
            reason="SPATIUM_INSECURE_SKIP_TLS_VERIFY=1",
            hint=(
                "Control-plane TLS verification is DISABLED for the "
                "lifetime of this supervisor process. Intended only for "
                "dev appliances pointed at a self-signed control plane; "
                "set the env to 0 / unset on production deployments."
            ),
        )
    # #272 Phase 1 — follow_redirects=True so an operator-typed
    # http:// CONTROL_PLANE_URL doesn't 301-loop against the nginx
    # http→https redirect on the appliance frontend. POST redirects
    # are followed verbatim (httpx preserves the method on 301/302
    # by default for non-safe methods unless explicitly disabled).
    return httpx.Client(verify=not skip_tls_verify, follow_redirects=True)


def _self_bootstrap_or_skip(
    cfg: SupervisorConfig,
    variant: str,
    log: structlog.stdlib.BoundLogger,
) -> SupervisorConfig:
    """Mint a local pairing code via the in-cluster api Service and
    return a config carrying it.

    Only fires on the control-plane appliance where the
    installer wizard didn't capture a pairing code (the control plane
    IS local). The api gates the endpoint on (1) the host bind-mounted
    ``role-config:ROLE`` matching this claim and (2) no existing
    Appliance rows, so calling it from anywhere other than the local
    supervisor on a fresh install fails 403/409.

    On success we return a frozen copy of ``cfg`` with the new code +
    control-plane URL stamped in. On any failure (api not reachable,
    403, 409 because we're already registered, ...) we log + return
    the original cfg so the caller's normal "skipped" log paths fire.
    Idempotent — a transient 409 just means the existing register
    flow will kick in once the supervisor's cached_appliance_id is
    populated on the next loop iteration.
    """
    # In-cluster Service URL. The api Service is named
    # ``<release>-spatiumddi-api`` in the spatium namespace; the
    # appliance helm-chart release is ``spatium-control``.
    # Hardcoded here so the supervisor doesn't need an extra env var
    # for the discovery URL — multi-node HA (#272 later phases) can
    # generalise this when we add real promotion-flow plumbing.
    api_url = "http://spatium-control-spatiumddi-api.spatium.svc.cluster.local:8000"
    log.info("supervisor.self_bootstrap.attempting", variant=variant, api_url=api_url)
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{api_url}/api/v1/appliance/self-register-bootstrap",
                json={"appliance_variant": variant},
            )
    except httpx.HTTPError as exc:
        log.warning(
            "supervisor.self_bootstrap.transport_failed",
            error=str(exc),
            hint="will retry on next register-loop tick",
        )
        return cfg
    if resp.status_code == 409:
        log.info("supervisor.self_bootstrap.already_registered")
        return cfg
    if resp.status_code != 200:
        log.warning(
            "supervisor.self_bootstrap.refused",
            status=resp.status_code,
            body=resp.text[:200],
        )
        return cfg
    try:
        payload = resp.json()
        code = payload["code"]
        control_plane_url = payload["control_plane_url"]
    except (ValueError, KeyError) as exc:
        log.warning("supervisor.self_bootstrap.malformed_response", error=str(exc))
        return cfg
    log.info(
        "supervisor.self_bootstrap.minted",
        variant=variant,
        code_last_two=code[-2:],
        control_plane_url=control_plane_url,
    )
    return dataclasses.replace(
        cfg,
        bootstrap_pairing_code=code,
        control_plane_url=control_plane_url,
    )


def _maybe_register(cfg: SupervisorConfig, log: structlog.stdlib.BoundLogger) -> SupervisorConfig:
    """Run identity generation + register-if-needed in one shot. Logs
    its own status; never raises into the caller (the main loop
    falls back to idle on any failure).

    Returns the (possibly mutated) ``cfg`` — when the
    self-bootstrap path fires on the control-plane node,
    ``control_plane_url`` and ``bootstrap_pairing_code`` are
    refreshed in the returned config so the caller's heartbeat
    loop can use the new control-plane URL going forward.
    Otherwise returns the input verbatim.
    """
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
        # Issue #170 Wave E follow-up + #272 Phase 1 audit — revoke
        # recovery. If the supervisor flipped to
        # ``approval-state=revoked`` (control plane returning 403/404
        # for our cached identity) we need to clear the stale soft
        # state (appliance_id / session_token / cert.pem / approval-
        # state / strikes) so the register call below mints a fresh
        # identity. The Ed25519 keypair stays — it's stable across
        # re-pairs and the control plane creates a new appliance row
        # + cert against it on the next approve.
        #
        # Re-pair sources, in order of precedence:
        #   1. ``cfg.bootstrap_pairing_code`` — operator handed us a
        #      fresh code via spatium-pair (#170 Wave E flow).
        #   2. Self-bootstrap variant (control-plane) —
        #      #272 Phase 1; the supervisor mints its own pairing
        #      code against the in-cluster api on the recovery path.
        #
        # Without case 2 the supervisor would stay locked in revoked
        # after the operator deleted its row from the Fleet UI on a
        # control-plane appliance (no pairing code env present, no
        # human-mediated recovery path) — verified live on .199.
        variant = appliance_state.detect_appliance_variant()
        can_self_bootstrap = variant == "control-plane"
        if approval_state.read_state(cfg.state_dir) == "revoked" and (
            cfg.bootstrap_pairing_code or can_self_bootstrap
        ):
            log.info(
                "supervisor.register.revoked_reregister",
                stale_appliance_id=str(cached_appliance_id),
                via_self_bootstrap=can_self_bootstrap and not cfg.bootstrap_pairing_code,
            )
            clear_appliance_id(cfg.state_dir)
            clear_session_token(cfg.state_dir)
            clear_cert(cfg.state_dir)
            approval_state.clear(cfg.state_dir)
            # Fall through to the register call below.
        else:
            log.info(
                "supervisor.register.cached",
                appliance_id=str(cached_appliance_id),
            )
            return cfg

    # #272 — self-bootstrap on the control-plane node.
    # The installer wizard doesn't capture a pairing code for these
    # variants (the control plane is local), so on first boot both
    # control_plane_url and bootstrap_pairing_code are empty. Try
    # the in-cluster api's self-register-bootstrap endpoint first;
    # on success the resulting code is reused as a normal pairing
    # code through the standard register flow below.
    if not cfg.control_plane_url and not cfg.bootstrap_pairing_code:
        variant = appliance_state.detect_appliance_variant()
        if variant == "control-plane":
            cfg = _self_bootstrap_or_skip(cfg, variant, log)

    if not cfg.control_plane_url:
        log.warning("supervisor.register.skipped", reason="no control_plane_url")
        return cfg
    if not cfg.bootstrap_pairing_code:
        log.warning("supervisor.register.skipped", reason="no bootstrap_pairing_code")
        return cfg

    skip_tls = os.environ.get("SPATIUM_INSECURE_SKIP_TLS_VERIFY", "").lower() in (
        "1",
        "true",
        "yes",
    )
    try:
        with _build_http_client(skip_tls_verify=skip_tls, log=log) as client:
            result = register(
                control_plane_url=cfg.control_plane_url,
                pairing_code=cfg.bootstrap_pairing_code,
                identity=identity,
                hostname=cfg.hostname,
                supervisor_version=_supervisor_version(),
                client=client,
                # #272 Phase 1 — let the control plane stamp the variant
                # + auto-assign fixed roles at register time instead of
                # waiting for the first heartbeat. None on docker / k8s
                # supervisors (no role-config bind mount).
                appliance_variant=appliance_state.detect_appliance_variant(),
            )
    except RegisterDisabled as exc:
        log.warning("supervisor.register.disabled", reason=str(exc))
        return cfg
    except RegisterFatal as exc:
        log.error("supervisor.register.fatal", reason=str(exc))
        return cfg

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
    return cfg


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

    cfg = _maybe_register(cfg, log)

    # Issue #183 Phase 4 — k3s proxy thread. Daemon thread that
    # long-polls the control plane for queued kubeapi requests +
    # forwards them to the local k3s apiserver. Self-resilient
    # (no cert / no registration / no k3s → sleep + retry); safe
    # to spawn even on legacy compose deployments. The proxy is
    # net-new for #183; pre-#183 control planes don't enqueue
    # anything so the loop just sees empty polls.
    if cfg.k8s_proxy_enabled:
        identity_for_proxy, _ = load_or_generate(cfg.state_dir)
        start_proxy_thread(cfg, identity_for_proxy)
    else:
        log.info("supervisor.k8s_proxy.disabled_by_config")

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
    # #170 Wave E — external watchdog liveness anchor. The host-side
    # ``spatiumddi-supervisor-watchdog.timer`` reads this file's
    # mtime every 2 min and force-restarts the supervisor container
    # if the loop hasn't ticked in >5 min. Stamped at the TOP of
    # every iteration (not just after a successful heartbeat) so a
    # transient control-plane outage doesn't trigger an unnecessary
    # restart — the loop is alive even when ``heartbeat_once`` fails.
    # ``touch`` semantics: open + close to update mtime; cheap on a
    # 1-CPU VM and a stuck loop simply stops doing it.
    liveness_path = cfg.state_dir / "last-loop-at"

    while not stop:
        try:
            liveness_path.touch()
        except OSError as exc:
            log.warning("supervisor.liveness.touch_failed", error=str(exc))
        appliance_id = load_appliance_id(cfg.state_dir)
        # #272 Phase 1 — retry the register path each loop iteration
        # while we haven't successfully registered. Catches the
        # control-plane self-bootstrap case where the
        # in-cluster api Service wasn't reachable on the supervisor's
        # first attempt at startup (api pod still coming up). Cheap
        # for the steady-state — cached_appliance_id short-circuits
        # ``_maybe_register`` on its first line.
        if appliance_id is None:
            cfg = _maybe_register(cfg, log)
            appliance_id = load_appliance_id(cfg.state_dir)
        if appliance_id is not None and cfg.control_plane_url:
            session_token = load_session_token(cfg.state_dir)
            identity, _ = load_or_generate(cfg.state_dir)
            try:
                with _build_http_client(skip_tls_verify=skip_tls, log=log) as client:
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
                reason=("no_appliance_id" if appliance_id is None else "no_control_plane_url"),
            )
        for _ in range(cfg.heartbeat_interval_seconds):
            if stop:
                break
            time.sleep(1)

    log.info("supervisor.stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())

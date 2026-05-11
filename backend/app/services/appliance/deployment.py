"""Cert deployer — writes the active cert to disk + reloads nginx.

Phase 4b.2 (issue #134). Called by:

* ``ensure_self_signed_cert`` on appliance api startup — populates the
  cert volume on first boot before the frontend container reads it.
* ``activate_certificate`` / ``import_signed_cert`` endpoints — pushes
  the freshly-activated cert into the volume + signals nginx to swap
  its TLS context.

Designed to be fully no-op on non-appliance deploys: if
``settings.appliance_mode`` is false we never touch the filesystem
or docker. On appliance deploys but during dev iteration (cert dir
not yet mounted, docker socket missing, frontend container not
running) the operations log + skip — they don't propagate failures
to the API caller, because by the time the deployer runs the DB has
already been committed (the cert IS active, the deployment is just
the materialisation).
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# File names inside ``appliance_cert_dir``. The frontend nginx config
# (mkosi.extra/usr/local/share/spatiumddi/nginx-appliance.conf) loads
# these exact paths.
_CERT_FILENAME = "active.pem"
_KEY_FILENAME = "active.key"
# Path the cert deployer treats as "is the docker socket reachable
# from this container". The deployer no-ops gracefully if it's not.
_DOCKER_SOCK = Path("/var/run/docker.sock")


def deploy_active_cert(cert_pem: str, key_pem: str, *, name: str = "") -> bool:
    """Write the cert + key to ``appliance_cert_dir``.

    Atomicity: writes to a ``.new`` sibling then ``os.replace``s on
    top of the live file. nginx reads happen via inotify-less
    SIGHUP — we never observe a half-written file from the frontend
    container's perspective because ``rename`` on the same filesystem
    is atomic, and SIGHUP runs after our rename completes.

    Returns True if the files were written; False if appliance_mode
    is off or the cert dir doesn't exist (logged either way).
    """
    if not settings.appliance_mode:
        return False

    cert_dir = Path(settings.appliance_cert_dir)
    try:
        cert_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as exc:
        logger.warning(
            "appliance_cert_deploy_dir_failed",
            cert_dir=str(cert_dir),
            error=str(exc),
        )
        return False

    cert_path = cert_dir / _CERT_FILENAME
    key_path = cert_dir / _KEY_FILENAME
    cert_tmp = cert_dir / (_CERT_FILENAME + ".new")
    key_tmp = cert_dir / (_KEY_FILENAME + ".new")

    try:
        cert_tmp.write_text(cert_pem, encoding="utf-8")
        key_tmp.write_text(key_pem, encoding="utf-8")
        # Cert is public — 0644 so nginx (running as nginx user) can
        # read it. Key MUST be 0600 even though nginx-the-process
        # opens it before dropping privileges; this stops casual cat
        # of the key from a sidecar / wrong-uid context.
        os.chmod(cert_tmp, 0o644)
        os.chmod(key_tmp, 0o600)
        os.replace(cert_tmp, cert_path)
        os.replace(key_tmp, key_path)
    except (PermissionError, OSError) as exc:
        logger.error(
            "appliance_cert_deploy_write_failed",
            cert_dir=str(cert_dir),
            name=name,
            error=str(exc),
        )
        return False

    logger.info(
        "appliance_cert_deployed",
        cert_dir=str(cert_dir),
        name=name,
    )
    return True


def reload_frontend_nginx() -> bool:
    """Send SIGHUP to the frontend container so nginx reloads.

    Uses the docker SDK over the host's docker socket. SIGHUP makes
    nginx re-read its config + cert files without dropping in-flight
    connections (graceful reload). No-op + logged warning if any of:
        - settings.appliance_mode is false
        - docker socket isn't mounted into this container
        - docker SDK isn't installed (e.g. dev image without the dep)
        - frontend container can't be found

    Returns True on success, False otherwise.
    """
    if not settings.appliance_mode:
        return False
    if not _DOCKER_SOCK.exists():
        logger.warning(
            "appliance_nginx_reload_no_docker_sock",
            socket=str(_DOCKER_SOCK),
        )
        return False

    try:
        import docker  # noqa: PLC0415 — optional in dev
        from docker.errors import (  # noqa: PLC0415
            APIError,
            DockerException,
            NotFound,
        )
    except ImportError as exc:
        logger.warning("appliance_nginx_reload_docker_sdk_missing", error=str(exc))
        return False

    try:
        client = docker.from_env()
    except DockerException as exc:
        logger.warning("appliance_nginx_reload_client_failed", error=str(exc))
        return False

    container_name = settings.appliance_frontend_container
    try:
        container = client.containers.get(container_name)
    except NotFound:
        logger.warning(
            "appliance_nginx_reload_container_not_found",
            container=container_name,
        )
        return False
    except APIError as exc:
        logger.warning(
            "appliance_nginx_reload_lookup_failed",
            container=container_name,
            error=str(exc),
        )
        return False

    try:
        # SIGHUP triggers nginx master process to re-read config + certs
        # while leaving existing workers handling in-flight requests.
        # Equivalent to ``nginx -s reload`` from inside the container.
        container.kill(signal="HUP")
    except APIError as exc:
        logger.warning(
            "appliance_nginx_reload_signal_failed",
            container=container_name,
            error=str(exc),
        )
        return False

    logger.info("appliance_nginx_reloaded", container=container_name)
    return True


def deploy_and_reload(cert_pem: str, key_pem: str, *, name: str = "") -> bool:
    """Convenience — deploy cert files, then signal nginx to reload.

    Returns True only when both steps succeeded. Failed deploys skip
    the reload (no point signalling nginx when the cert on disk
    didn't change).
    """
    if not deploy_active_cert(cert_pem, key_pem, name=name):
        return False
    return reload_frontend_nginx()

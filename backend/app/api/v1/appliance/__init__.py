"""Appliance management surface — Phase 4 of issue #134.

Mounted at ``/api/v1/appliance``. Lit up by ``settings.appliance_mode``
on installs that booted from the SpatiumDDI OS appliance ISO. On plain
docker-compose / Kubernetes deploys the router stays mounted but its
endpoints either 404 or return empty data because the underlying host
surfaces (systemd, docker socket, nftables, /etc/spatiumddi/.env) aren't
there to drive.

Sub-phases land as separate files inside this package:

* ``router.py``                — read-only frame + /info (Phase 4a, this commit)
* ``tls.py``      (Phase 4b)  — TLS cert upload / Let's Encrypt / CSR
* ``releases.py`` (Phase 4c)  — release manager + rollback
* ``containers.py`` (Phase 4d) — container list, start/stop/restart, live logs
* ``logs.py``     (Phase 4e)  — system log viewer + diagnostic bundle + self-test
* ``network.py``  (Phase 4f)  — host network + firewall + SSH key + maintenance
* ``wizard.py``   (Phase 4g)  — web first-boot wizard + recovery
"""

from app.api.v1.appliance.router import router

__all__ = ["router"]

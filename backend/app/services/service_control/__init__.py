"""One lifecycle-control surface across every deployment shape (issue #890).

Before this, restarting a SpatiumDDI service from the web UI worked on
exactly one deployment: the k3s appliance, through
``/appliance/containers/{name}/{action}``. Docker-compose had read-only
stats over ``docker.sock``; the umbrella Helm chart had nothing. The
docs described a Start/Stop/Restart surface spanning Docker, Kubernetes
and bare-metal SSH ``systemctl`` that had never been written.

This package answers the capability question *first* — ``capability()``
reports which lifecycle backend is live and what it can do — so the UI
renders what actually exists instead of discovering it from a 503.

Two backends, deliberately different in what they offer:

* **kubernetes** — ``restart`` only, as a rollout restart (bumping
  ``kubectl.kubernetes.io/restartedAt`` on the pod template). ``start`` /
  ``stop`` would mean scaling a workload to zero and back, which needs
  somewhere durable to remember the previous replica count; a control
  plane that forgets is worse than one that never offered.
* **compose** — ``start`` / ``stop`` / ``restart``, since the Docker
  daemon models exactly that.

Both are **allowlisted against the live inventory**: an action names a
service from ``list_services()`` and is resolved against that list
server-side. An id that isn't in the inventory is a 404, so no
caller-supplied string ever reaches the daemon as a container name or a
workload path.

Gating (see ``settings.service_control_enabled``): off by default on
compose and plain Kubernetes, because a restart-capable ``docker.sock``
is host-root-equivalent and the Kubernetes path needs RBAC the chart
only grants when asked. On the appliance it is on, matching the control
that endpoint already shipped.
"""

from app.services.service_control.backends import (
    ACTIONS,
    ServiceControlCapability,
    ServiceControlError,
    ServiceSummary,
    apply_action,
    capability,
    list_services,
)

__all__ = [
    "ACTIONS",
    "ServiceControlCapability",
    "ServiceControlError",
    "ServiceSummary",
    "apply_action",
    "capability",
    "list_services",
]

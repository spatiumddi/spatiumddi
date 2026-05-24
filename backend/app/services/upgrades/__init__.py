"""Multi-node rolling-upgrade machinery (#296).

Phase A scope (this directory):

* ``preflight.py`` — read-only safety checks (quorum, replication
  lag, disk headroom, version path, in-flight conflicts). The
  endpoint at ``GET /api/v1/upgrades/preflight?target=<tag>`` returns
  the aggregate without mutating cluster state.
* ``mutex.py`` — cluster-wide single-upgrader Lease helper built on
  ``coordination.k8s.io/v1/Lease``. Phase A ships the helper; the
  orchestrator that uses it lands in Phase D.

Phase C will add ``per_node.py`` (the verified cordon → drain →
apply → reboot → health-gate → uncordon primitive); Phase D will
add ``orchestrator.py`` (the cluster-wide rolling driver).
"""

from __future__ import annotations

"""Spatium appliance supervisor.

Phase A1 — scaffolding only. The supervisor owns every host-side
concern on an Application-role appliance: identity (Ed25519 keypair +
control-plane-signed cert), docker compose orchestration of service
containers, nftables drop-in management, and slot / system telemetry
reporting. None of that is implemented yet; this module currently
boots, logs its idle state, and sleeps.

See https://github.com/spatiumddi/spatiumddi/issues/170 for the full
design.
"""

__version__ = "2026.05.14.1"

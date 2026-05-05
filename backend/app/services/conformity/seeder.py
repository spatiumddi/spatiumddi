"""Built-in conformity policy library.

Seeded on first boot, all rows ``is_builtin=True`` and ``enabled=False``
so operators opt in (dropping a flag they don't actually use into
the periodic evaluator wastes cycles + clutters the dashboard).

Seeding rules:

* Identity is ``(framework, name)`` — once a row exists with that
  pair the seeder leaves it alone, even if the operator renamed
  / disabled / re-targeted it. This means evolving the seed file
  doesn't fight the operator's customisations.
* Adding a new policy ships immediately on the next deploy.
* Removing a seed entry does NOT delete the existing row in the
  database — the operator owns the lifecycle once the row lands.
* The ``check_args`` dict is what the per-check evaluator reads;
  schema is per-check (see ``app.services.conformity.checks``).
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.conformity import ConformityPolicy

logger = structlog.get_logger(__name__)


_BUILTIN_POLICIES: list[dict[str, object]] = [
    # ── PCI-DSS 4.0 ─────────────────────────────────────────────────
    {
        "name": "PCI subnets must be in a dedicated VRF",
        "description": (
            "Cardholder-data networks must be routed in their own VRF — "
            "no co-tenancy with non-PCI siblings. Fails when the "
            "subnet's effective VRF holds non-flagged neighbours."
        ),
        "framework": "PCI-DSS 4.0",
        "reference": "1.2.1",
        "severity": "critical",
        "target_kind": "subnet",
        "target_filter": {"classification": "pci_scope"},
        "check_kind": "in_separate_vrf",
        "check_args": {"classification": "pci_scope"},
    },
    {
        "name": "PCI subnets must have an owner (customer_id)",
        "description": (
            "Every cardholder-data subnet needs a recorded owner so the "
            "auditor can trace responsibility. Drives the customer_id "
            "FK shipped in #91."
        ),
        "framework": "PCI-DSS 4.0",
        "reference": "12.4.1",
        "severity": "warning",
        "target_kind": "subnet",
        "target_filter": {"classification": "pci_scope"},
        "check_kind": "has_field",
        "check_args": {"field": "customer_id"},
    },
    {
        "name": "PCI hosts must not expose admin ports",
        "description": (
            "Per latest nmap scan no IP in PCI scope may have 22 / 23 / "
            "3389 reachable. Soft-warns when no scan exists in the last "
            "30 days — operators see 'we don't know yet' rather than a "
            "false pass."
        ),
        "framework": "PCI-DSS 4.0",
        "reference": "1.4.4",
        "severity": "critical",
        "target_kind": "ip_address",
        "target_filter": {"classification": "pci_scope"},
        "check_kind": "no_open_ports",
        "check_args": {"ports": [22, 23, 3389], "max_age_days": 30},
    },
    {
        "name": "PCI scope changes must be alerted on",
        "description": (
            "An enabled compliance_change alert rule with classification "
            "= pci_scope must exist. Confirms the reactive signal "
            "(#105) is wired to a delivery target."
        ),
        "framework": "PCI-DSS 4.0",
        "reference": "10.7.2",
        "severity": "warning",
        "target_kind": "platform",
        "target_filter": {},
        "check_kind": "alert_rule_covers",
        "check_args": {
            "rule_type": "compliance_change",
            "classification": "pci_scope",
        },
    },
    {
        "name": "PCI subnets must not contain stale IP rows",
        "description": (
            "Every IP in a cardholder-data subnet must have been seen "
            "(via nmap, DHCP, or device discovery) within the last 30 "
            "days. Catches rows that should be decommissioned."
        ),
        "framework": "PCI-DSS 4.0",
        "reference": "9.4.6",
        "severity": "warning",
        "target_kind": "subnet",
        "target_filter": {"classification": "pci_scope"},
        "check_kind": "last_seen_within",
        "check_args": {"max_age_days": 30},
    },
    # ── HIPAA ───────────────────────────────────────────────────────
    {
        "name": "HIPAA subnets must be in a dedicated VRF",
        "description": (
            "ePHI networks must be isolated at the routing layer. Fails "
            "when the effective VRF holds non-HIPAA siblings."
        ),
        "framework": "HIPAA",
        "reference": "164.312(a)(1)",
        "severity": "critical",
        "target_kind": "subnet",
        "target_filter": {"classification": "hipaa_scope"},
        "check_kind": "in_separate_vrf",
        "check_args": {"classification": "hipaa_scope"},
    },
    # ── Internet-facing ─────────────────────────────────────────────
    {
        "name": "Internet-facing scope must be alerted on",
        "description": (
            "An enabled compliance_change alert rule with classification "
            "= internet_facing must exist."
        ),
        "framework": "custom",
        "reference": None,
        "severity": "warning",
        "target_kind": "platform",
        "target_filter": {},
        "check_kind": "alert_rule_covers",
        "check_args": {
            "rule_type": "compliance_change",
            "classification": "internet_facing",
        },
    },
    # ── Platform integrity ──────────────────────────────────────────
    {
        "name": "Audit log is reachable and append-only",
        "description": (
            "Confirms the audit_log table is queryable. SpatiumDDI's "
            "DB trigger guards against DELETE — this check is the "
            "presence signal that gets cited in the auditor PDF."
        ),
        "framework": "SOC2",
        "reference": "CC7.1",
        "severity": "info",
        "target_kind": "platform",
        "target_filter": {},
        "check_kind": "audit_log_immutable",
        "check_args": {},
    },
]


async def seed_builtin_conformity_policies() -> None:
    """Insert built-in policies on first start. Idempotent.

    Match key is ``(framework, name)`` so renaming a row in the
    seed list creates a fresh row alongside the operator's old one
    rather than silently overwriting their settings.
    """
    async with AsyncSessionLocal() as session:
        for spec in _BUILTIN_POLICIES:
            existing = await session.scalar(
                select(ConformityPolicy).where(
                    ConformityPolicy.framework == spec["framework"],
                    ConformityPolicy.name == spec["name"],
                )
            )
            if existing is not None:
                continue
            session.add(
                ConformityPolicy(
                    name=spec["name"],
                    description=spec["description"],
                    framework=spec["framework"],
                    reference=spec["reference"],
                    severity=spec["severity"],
                    target_kind=spec["target_kind"],
                    target_filter=spec["target_filter"],
                    check_kind=spec["check_kind"],
                    check_args=spec["check_args"],
                    is_builtin=True,
                    enabled=False,
                    eval_interval_hours=24,
                )
            )
        await session.commit()


__all__ = ["seed_builtin_conformity_policies"]

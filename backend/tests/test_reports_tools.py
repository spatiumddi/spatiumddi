"""Top-N report Operator-Copilot tool tests (issue #47).

Covers:

* Each ``find_top_*`` tool is registered, read-only, and carries the
  ``reports.top_n`` feature-module tag.
* Tools return capped, ranked rows (reusing the same aggregation the
  REST endpoints use).
* The tools are stripped from the effective set when ``reports.top_n``
  is disabled (``effective_tool_names`` with that module excluded).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ipam import IPBlock, IPSpace, Subnet
from app.services.ai.tools import REGISTRY, effective_tool_names
from app.services.ai.tools.reports import (
    FindTopSubnetsArgs,
    find_top_subnets_by_utilization,
)
from app.services.feature_modules import all_module_ids

_TOOL_NAMES = (
    "find_top_subnets_by_utilization",
    "find_top_owners_by_ip_count",
    "find_top_modified_resources",
    "find_top_dns_clients",
)


def test_tools_registered_with_expected_flags() -> None:
    for name in _TOOL_NAMES:
        tool = REGISTRY.get(name)
        assert tool is not None, f"{name} not registered"
        assert tool.writes is False
        assert tool.default_enabled is True
        assert tool.module == "reports.top_n"
        assert tool.category == "reports"


def test_module_is_in_catalog() -> None:
    # The tools tag a module that must actually exist in the catalog,
    # else effective_tool_names can never resolve the gate.
    assert "reports.top_n" in all_module_ids()


def test_tools_in_effective_set_when_module_enabled() -> None:
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules={"reports.top_n"},
    )
    for name in _TOOL_NAMES:
        assert name in enabled


def test_tools_stripped_when_module_disabled() -> None:
    # Every module enabled EXCEPT reports.top_n.
    others = all_module_ids() - {"reports.top_n"}
    enabled = effective_tool_names(
        platform_enabled=None,
        provider_enabled=None,
        enabled_modules=others,
    )
    for name in _TOOL_NAMES:
        assert name not in enabled


@pytest.mark.asyncio
async def test_find_top_subnets_returns_capped_ranked_rows(db_session: AsyncSession) -> None:
    sp = IPSpace(name=f"space-{uuid.uuid4().hex[:8]}")
    db_session.add(sp)
    await db_session.flush()
    blk = IPBlock(space_id=sp.id, network="10.0.0.0/8", name="blk")
    db_session.add(blk)
    await db_session.flush()
    for i in range(15):
        db_session.add(
            Subnet(
                space_id=sp.id,
                block_id=blk.id,
                network=f"10.0.{i}.0/24",
                name=f"s{i}",
                utilization_percent=float(i),
                allocated_ips=i,
                total_ips=256,
            )
        )
    await db_session.flush()

    # No User row needed — the tool ignores ``user`` (read-only).
    rows = await find_top_subnets_by_utilization(db_session, None, FindTopSubnetsArgs())  # type: ignore[arg-type]
    assert len(rows) == 10
    utils = [r["utilization_percent"] for r in rows]
    assert utils == sorted(utils, reverse=True)
    assert utils[0] == 14.0


@pytest.mark.asyncio
async def test_find_top_subnets_respects_limit_arg(db_session: AsyncSession) -> None:
    sp = IPSpace(name=f"space-{uuid.uuid4().hex[:8]}")
    db_session.add(sp)
    await db_session.flush()
    blk = IPBlock(space_id=sp.id, network="172.16.0.0/12", name="blk")
    db_session.add(blk)
    await db_session.flush()
    for i in range(5):
        db_session.add(
            Subnet(
                space_id=sp.id,
                block_id=blk.id,
                network=f"172.16.{i}.0/24",
                utilization_percent=float(i),
            )
        )
    await db_session.flush()

    rows = await find_top_subnets_by_utilization(
        db_session, None, FindTopSubnetsArgs(limit=3)  # type: ignore[arg-type]
    )
    assert len(rows) == 3

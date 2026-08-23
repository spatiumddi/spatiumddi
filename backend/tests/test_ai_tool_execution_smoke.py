"""Every read-only copilot tool must actually run (#923).

A tool is a plain async function reached only through the registry, so a
reference to a column the model doesn't have — ``DHCPServer.group_id``,
``NetworkDevice.last_polled_at``, ``DHCPServerGroup.ddns_enabled`` — is not a
filter that silently misbehaves. It is an ``AttributeError`` the first time
the line runs, which means the tool answers **nothing, for every input**, and
has never worked since it shipped. ``test_model_attribute_references`` catches
the ``Model.attr`` spelling in a query; it cannot see ``row.attr`` while
building a response dict, and half the instances found in #923 were that
second kind. Only executing the tool finds those.

So this invokes every registered read-only tool against an empty database
with default arguments. An empty result is a pass — the assertion is that the
call *returns*.

Each tool runs inside its **own SAVEPOINT**. That is not tidiness: a failed
statement leaves Postgres refusing everything until rollback, and rolling the
whole session back instead would expire the shared objects, so every tool
after the first failure reports ``MissingGreenlet`` and buries the one real
finding. It is also why this is a single test rather than one parametrised
case per tool — the ``db_session`` fixture truncates every mapped table
between tests, and 300 of those exhausted memory on an 8-core dev box before
the suite finished.

Tools whose arguments have no defaults are skipped — there is nothing
truthful to invent for a required id, and passing a fabricated one would test
the not-found path rather than the tool.

**Known limit, stated rather than implied:** an empty database exercises each
tool's query and its empty-result path, not every branch that builds a
response row. ``list_platform_health``'s ``Subnet.cidr`` — also a #923
finding — only runs once some subnet crosses 80% utilisation, so this check
cannot see it and the AST guard cannot either. The third lens for those is
``mypy --enable-error-code attr-defined``, run by hand: ``attr-defined`` is in
``disable_error_code`` repo-wide, and roughly thirty of its findings are
false positives from dynamic-model patterns (``type`` / ``Base`` typed loop
variables, duck-typed driver calls), so it is a periodic sweep rather than a
gate.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.auth import User
from app.services.ai.tools import REGISTRY


def test_registry_is_populated() -> None:
    """A registry that failed to import would make the sweep below vacuous."""
    assert len([t for t in REGISTRY.all() if not t.writes]) > 100


@pytest.mark.asyncio
async def test_every_read_only_tool_executes(db_session: AsyncSession) -> None:
    user = User(
        username=f"tool-smoke-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        display_name="Tool Smoke",
        hashed_password=hash_password("x"),
        is_superadmin=True,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    failures: list[str] = []
    skipped = 0
    ran = 0
    for tool in sorted(REGISTRY.all(), key=lambda t: t.name):
        if tool.writes:
            continue
        try:
            args = tool.args_model.model_validate({})
        except Exception:
            skipped += 1
            continue
        savepoint = await db_session.begin_nested()
        try:
            await tool.executor(db_session, user, args)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — every failure is a result here
            failures.append(f"{tool.name}: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            if savepoint.is_active:
                await savepoint.rollback()

    assert ran > 50, f"only {ran} tools were invokable — argument defaults changed?"
    assert not failures, "read-only tools that raised:\n  " + "\n  ".join(failures)

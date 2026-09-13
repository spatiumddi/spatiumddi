"""A dependency that did not answer is a 503, not a 500 (#1083).

asyncpg raises the bare ``TimeoutError`` for a connect or a command that ran
past ``db.py``'s ``connect_args`` bounds, and SQLAlchemy's asyncpg adapter
re-raises anything that is not an asyncpg error class untranslated, so the
``InterfaceError``/``OperationalError`` handler never saw it: during a CNPG
failover every request whose session lookup needed a fresh connection
answered ``500 Internal Server Error`` for the 60-90 s the promotion took.
These pin the handler that turns that window into a ``503`` with a
``Retry-After`` — called directly, so no database is needed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError
from starlette.requests import Request

from app.main import app

pytestmark = pytest.mark.asyncio


def _request(path: str = "/api/v1/appliance/cluster/health") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def _handler_for(exc: BaseException):
    """Resolve the handler the way Starlette's ExceptionMiddleware does:
    the first registered class in the exception's MRO."""
    for cls in type(exc).__mro__:
        if cls in app.exception_handlers:
            return app.exception_handlers[cls]
    raise AssertionError(f"no handler registered for {type(exc).__name__}")


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("connect timed out"),
        TimeoutError("wait_for timed out"),
        ConnectionRefusedError(111, "Connection refused"),
        ConnectionResetError(104, "Connection reset by peer"),
        SAPoolTimeoutError("QueuePool limit of size 5 overflow 10 reached, connection timed out"),
    ],
    ids=["asyncpg-connect-timeout", "wait-for-timeout", "refused", "reset", "pool-checkout"],
)
async def test_dependency_timeouts_answer_503_with_retry_after(exc: BaseException) -> None:
    handler = _handler_for(exc)
    assert (
        handler is not app.exception_handlers[Exception]
    ), f"{type(exc).__name__} would fall through to the unhandled-exception 500"
    resp = await handler(_request(), exc)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "2"
    body = json.loads(resp.body)
    assert body["detail"].startswith("A backend dependency did not answer in time")


async def test_a_real_bug_still_reaches_the_500_path() -> None:
    """The handler must not swallow programming errors: a ValueError keeps
    the unhandled-exception capture and the 500."""
    handler = _handler_for(ValueError("bug"))
    assert handler is app.exception_handlers[Exception]

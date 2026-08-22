"""SpatiumDDI control-plane application package.

Deliberately not empty: ``install()`` below has to run before the first
pydantic model class in this package is created, and package ``__init__`` is
the only import site that guarantees it. Python executes this module before
any ``app.*`` submodule, in every entrypoint we have — uvicorn (``app.main``),
the Celery worker and beat (``app.celery_app``), alembic's ``env.py``, the
OpenAPI exporter and every test.

Putting the call at the top of ``app/main.py`` instead would look equivalent
and silently not be: isort sorts ``from app.core.json_datetime import …``
after ``from app.api.v1.router import …``, so the routers — and every model
they define — would already be imported by the time it ran.
"""

from app.core.json_datetime import install as _install_rfc3339_ms_timestamps

# Issue #907: pin every JSON-serialised datetime to RFC 3339 with exactly
# three fractional digits. See app/core/json_datetime.py for why this is a
# schema-builder patch rather than 714 field annotations.
_install_rfc3339_ms_timestamps()

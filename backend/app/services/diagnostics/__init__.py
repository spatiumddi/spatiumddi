"""Diagnostics surface — captures uncaught exceptions for the
admin Diagnostics → Errors page (issue #123)."""

from app.services.diagnostics.capture import (
    record_unhandled_exception,
    record_unhandled_exception_async,
)

__all__ = [
    "record_unhandled_exception",
    "record_unhandled_exception_async",
]

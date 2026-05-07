"""Backup destination ABC + registry (issue #117 Phase 1b).

A ``BackupDestination`` is the boundary between
``app.services.backup.archive`` (which builds a zip in memory) and
the storage backend (local volume, S3, SCP, Azure Blob, …). Each
driver implements four operations:

* :meth:`write` — accept ``bytes`` + filename, persist them.
* :meth:`list_archives` — return the stored archives newest-first.
* :meth:`delete` — drop one archive by filename. Used by the
  retention sweep + the manual delete UI.
* :meth:`test_connection` — write a tiny probe file, list it,
  delete it. Mirrors the existing DNS / DHCP server probe pattern.

The ABC also declares :attr:`config_fields` — the per-kind
config-shape descriptor the API layer uses to validate the
``config`` JSONB blob on create / update without baking knowledge
of every driver into the router.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class BackupDestinationError(Exception):
    """Generic failure inside a destination driver — wraps the
    underlying client exception with a one-line message the API
    layer surfaces verbatim to operators."""


class DestinationConfigError(BackupDestinationError):
    """The ``config`` blob is malformed for this destination kind
    (missing a required field, type mismatch, etc.). 422-shaped on
    the API side."""


@dataclass(frozen=True)
class ArchiveListing:
    """One archive present at a destination."""

    filename: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class ConfigFieldSpec:
    """Descriptor for one field inside a destination's ``config``
    JSONB blob. The API + frontend reflect on these to render the
    per-kind form + validate input.
    """

    name: str
    label: str
    type: str  # text / password / number
    required: bool = True
    description: str | None = None
    secret: bool = False  # hide from list responses


class BackupDestination(ABC):
    """ABC for a backup destination. Subclasses are stateless;
    every operation receives the per-target ``config`` dict and
    handles its own per-call connection lifecycle.
    """

    #: Stable identifier — also the value of ``backup_target.kind``.
    kind: str

    #: Human-readable label rendered in the destination picker.
    label: str

    #: Per-kind config-field descriptors. Empty for destinations
    #: with no configurable fields beyond name/passphrase.
    config_fields: tuple[ConfigFieldSpec, ...] = ()

    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> None:
        """Raise :class:`DestinationConfigError` if ``config`` is
        missing a required field or has the wrong type. Called on
        every create / update + before every run.
        """

    @abstractmethod
    async def write(self, *, config: dict[str, Any], filename: str, archive_bytes: bytes) -> None:
        """Persist ``archive_bytes`` under ``filename`` at this
        destination. Must overwrite if a file with the same name
        already exists.
        """

    @abstractmethod
    async def list_archives(self, *, config: dict[str, Any]) -> list[ArchiveListing]:
        """Return all archives stored at this destination,
        newest-first by ``created_at``.
        """

    @abstractmethod
    async def delete(self, *, config: dict[str, Any], filename: str) -> None:
        """Drop ``filename`` from this destination. Idempotent —
        deleting a missing file should not raise.
        """

    @abstractmethod
    async def test_connection(self, *, config: dict[str, Any]) -> dict[str, Any]:
        """Write + list + delete a tiny probe file. Returns
        ``{ok: bool, error?: str, detail?: str}`` in the same shape
        as the existing DNS / DHCP probe surfaces.
        """


# Module-level driver registry. ``__init__.py`` populates it on
# import; the API layer looks drivers up by ``kind`` here.
DESTINATIONS: dict[str, BackupDestination] = {}


def get_destination(kind: str) -> BackupDestination:
    driver = DESTINATIONS.get(kind)
    if driver is None:
        raise DestinationConfigError(
            f"unknown backup destination kind: {kind!r} " f"(known: {sorted(DESTINATIONS)})"
        )
    return driver


def list_destination_kinds() -> list[dict[str, Any]]:
    """Return the full destination catalog for the frontend
    "pick a kind" picker. Each entry carries ``kind`` / ``label``
    plus the per-kind ``config_fields`` descriptors so the form
    can render itself.
    """
    out: list[dict[str, Any]] = []
    for kind in sorted(DESTINATIONS):
        d = DESTINATIONS[kind]
        out.append(
            {
                "kind": d.kind,
                "label": d.label,
                "config_fields": [
                    {
                        "name": f.name,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "description": f.description,
                        "secret": f.secret,
                    }
                    for f in d.config_fields
                ],
            }
        )
    return out

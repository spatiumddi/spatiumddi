"""Local-volume backup destination (issue #117 Phase 1b).

Writes archives to a directory on the api / worker container's
filesystem. The path is operator-configured; production
deployments should mount it into the container as a docker /
k8s volume so archives survive container recycle.

This is the simplest driver — no auth, no network, no transient
failure modes beyond filesystem permissions. Useful as:

* A first-class destination for installs that mount NFS / external
  storage at the configured path (the typical homelab + small
  enterprise pattern).
* The reference implementation everyone else in
  :mod:`app.services.backup.targets` will mirror — ``write`` /
  ``list_archives`` / ``delete`` / ``test_connection`` shape +
  ``validate_config`` semantics + the
  ``DestinationConfigError`` envelope.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.services.backup.targets.base import (
    ArchiveListing,
    BackupDestination,
    ConfigFieldSpec,
    DestinationConfigError,
)

logger = structlog.get_logger(__name__)

# Filename pattern emitted by ``build_backup_archive`` plus the
# pre-restore safety dump. Anything else in the directory is
# ignored by ``list_archives`` so an operator-mounted directory
# that also holds unrelated files stays clean.
_ARCHIVE_NAME_RE = re.compile(r"^(spatiumddi-backup-|pre-restore-).*\.zip$")

# Don't follow symlinks out of the configured root — operators
# who mount an external volume at the path expect "delete"
# scoped to that volume, not chasing a symlink to ``/etc``.
# ``Path.is_file(follow_symlinks=...)`` is 3.13+; we run on
# 3.12, so the listing pass below uses ``Path.is_symlink()``
# + ``Path.is_file()`` together instead of the kwarg.


class LocalVolumeDestination(BackupDestination):
    kind = "local_volume"
    label = "Local volume"
    config_fields = (
        ConfigFieldSpec(
            name="path",
            label="Filesystem path",
            type="text",
            required=True,
            description=(
                "Absolute path on the api/worker container. Mount "
                "this as a docker / k8s volume in production so "
                "archives survive container recycle. Default dev "
                "mount: /var/lib/spatiumddi/backups."
            ),
        ),
    )

    def validate_config(self, config: dict[str, Any]) -> None:
        path = config.get("path")
        if not path or not isinstance(path, str):
            raise DestinationConfigError("'path' is required and must be a string")
        if not path.startswith("/"):
            raise DestinationConfigError("'path' must be absolute (start with '/')")
        # Refuse obvious foot-guns. ``/`` and ``/etc`` aren't safe
        # roots; force the operator to pick something application-
        # scoped.
        forbidden_roots = {"/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/proc", "/sys"}
        if path.rstrip("/") in forbidden_roots:
            raise DestinationConfigError(
                f"'path' may not be a system root ({path!r}); pick an app-scoped directory"
            )

    def _path(self, config: dict[str, Any]) -> Path:
        self.validate_config(config)
        return Path(config["path"]).resolve()

    async def write(self, *, config: dict[str, Any], filename: str, archive_bytes: bytes) -> None:
        root = self._path(config)

        # Synchronous filesystem ops are fast on a local disk; we
        # offload to a thread anyway so a slow NFS mount can't
        # block the asyncio loop.
        def _do() -> None:
            root.mkdir(parents=True, exist_ok=True)
            target = root / _safe_name(filename)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(archive_bytes)
            # Atomic rename so partial writes are never visible to
            # the listing pass — important for the retention sweep
            # which keys on filename + size.
            os.replace(tmp, target)

        await asyncio.to_thread(_do)

    async def list_archives(self, *, config: dict[str, Any]) -> list[ArchiveListing]:
        root = self._path(config)

        def _do() -> list[ArchiveListing]:
            if not root.exists():
                return []
            rows: list[ArchiveListing] = []
            for entry in root.iterdir():
                # Skip symlinks explicitly so we never escape the
                # configured root, then check is_file() on what's
                # left. ``Path.lstat()`` returns the link's own
                # stat (not the target's) which is what we want
                # for size + mtime here.
                if entry.is_symlink() or not entry.is_file():
                    continue
                if not _ARCHIVE_NAME_RE.match(entry.name):
                    continue
                stat = entry.lstat()
                rows.append(
                    ArchiveListing(
                        filename=entry.name,
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    )
                )
            rows.sort(key=lambda r: r.created_at, reverse=True)
            return rows

        return await asyncio.to_thread(_do)

    async def delete(self, *, config: dict[str, Any], filename: str) -> None:
        root = self._path(config)
        safe = _safe_name(filename)

        def _do() -> None:
            target = root / safe
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "backup_local_volume_delete_failed",
                    path=str(target),
                    error=str(exc),
                )
                raise

        await asyncio.to_thread(_do)

    async def test_connection(self, *, config: dict[str, Any]) -> dict[str, Any]:
        try:
            root = self._path(config)
        except DestinationConfigError as exc:
            return {"ok": False, "error": str(exc)}
        # Probe file uses ``.bin`` so it can't be confused with a
        # real archive by ``list_archives`` (which filters to the
        # ``spatiumddi-backup-*.zip`` / ``pre-restore-*.zip``
        # patterns). Existence-check goes through ``Path.is_file``
        # directly rather than via ``list_archives``.
        probe_name = f"spatiumddi-test-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.bin"

        def _exists() -> bool:
            return (root / probe_name).is_file()

        try:
            await self.write(config=config, filename=probe_name, archive_bytes=os.urandom(16))
            present = await asyncio.to_thread(_exists)
            await self.delete(config=config, filename=probe_name)
        except (OSError, PermissionError) as exc:
            return {"ok": False, "error": f"filesystem: {exc}"}
        if not present:
            return {
                "ok": False,
                "error": (f"wrote probe but it didn't appear at {root} — check permissions"),
            }
        return {"ok": True, "detail": f"wrote + verified + deleted probe under {root}"}


def _safe_name(filename: str) -> str:
    """Strip path components from an operator-supplied filename so
    a malicious archive name like ``../../etc/passwd.zip`` can't
    escape the configured root.
    """
    return os.path.basename(filename)

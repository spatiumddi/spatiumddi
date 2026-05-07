"""Backup destinations (issue #117 Phase 1b+).

Each destination is a thin driver that knows how to ``write``,
``list``, ``delete``, and ``test_connection`` against its own
storage backend. Phase 1b ships ``local_volume`` only; ``s3`` /
``scp`` / ``azure_blob`` follow in 1c / 1d under the same
:class:`BackupDestination` ABC + module-level registry.
"""

from app.services.backup.targets.base import (
    DESTINATIONS,
    ArchiveListing,
    BackupDestination,
    BackupDestinationError,
    DestinationConfigError,
    get_destination,
    list_destination_kinds,
)
from app.services.backup.targets.local_volume import LocalVolumeDestination

# Side-effect register the local_volume driver. Future drivers
# import + register the same way.
DESTINATIONS["local_volume"] = LocalVolumeDestination()

__all__ = [
    "ArchiveListing",
    "BackupDestination",
    "BackupDestinationError",
    "DestinationConfigError",
    "LocalVolumeDestination",
    "DESTINATIONS",
    "get_destination",
    "list_destination_kinds",
]

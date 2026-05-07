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
from app.services.backup.targets.s3 import S3Destination
from app.services.backup.targets.secrets_config import (
    REDACTED_SENTINEL,
    SecretFieldError,
    decrypt_config_secrets,
    encrypt_config_secrets,
    merge_config_for_update,
    redact_config_secrets,
)

# Side-effect register every driver. Future drivers (SCP, Azure
# Blob in Phase 1d) import + register the same way.
DESTINATIONS["local_volume"] = LocalVolumeDestination()
DESTINATIONS["s3"] = S3Destination()

__all__ = [
    "ArchiveListing",
    "BackupDestination",
    "BackupDestinationError",
    "DestinationConfigError",
    "LocalVolumeDestination",
    "S3Destination",
    "DESTINATIONS",
    "REDACTED_SENTINEL",
    "SecretFieldError",
    "decrypt_config_secrets",
    "encrypt_config_secrets",
    "get_destination",
    "list_destination_kinds",
    "merge_config_for_update",
    "redact_config_secrets",
]

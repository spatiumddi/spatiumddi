"""Backup + restore service (issue #117 Phase 1a).

The shipped scope of Phase 1a is local download + local upload
restore, with a passphrase-wrapped ``secrets.enc`` envelope so the
operator can decrypt + re-apply the source install's
``SECRET_KEY`` to a fresh install if they want cross-install
portability. Remote destinations (S3 / SCP / Azure / SMB / FTP /
GCS) are deferred to Phase 1b/c.
"""

from app.services.backup.archive import (
    BackupArchiveError,
    build_backup_archive,
    read_backup_manifest,
)
from app.services.backup.crypto import (
    BackupCryptoError,
    decrypt_secrets,
    encrypt_secrets,
)
from app.services.backup.restore import (
    BackupRestoreError,
    apply_backup_restore,
)

__all__ = [
    "BackupArchiveError",
    "BackupCryptoError",
    "BackupRestoreError",
    "apply_backup_restore",
    "build_backup_archive",
    "decrypt_secrets",
    "encrypt_secrets",
    "read_backup_manifest",
]

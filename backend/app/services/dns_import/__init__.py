"""DNS configuration importer (issue #128).

Three sources, one canonical IR. Each source module parses the
upstream config (BIND9 archive, Windows DNS WinRM dump, PowerDNS
REST pull) into :class:`ImportPreview`; the shared
:func:`commit_import` writes the IR to the DB stamping
``import_source`` + ``imported_at`` on every row it creates so the
provenance is queryable later.

Phase 1 ships BIND9 only; Phase 2 + 3 add Windows DNS + PowerDNS.
"""

from .bind9 import ImportSourceError, parse_bind9_archive
from .canonical import (
    ConflictAction,
    ImportedRecord,
    ImportedSOA,
    ImportedZone,
    ImportPreview,
    ImportSource,
    ZoneConflict,
)
from .commit import CommitResult, CommitZoneResult, commit_import, detect_conflicts
from .powerdns import (
    PowerDNSImportError,
    parse_powerdns_server,
    test_powerdns_connection,
)
from .windows_dns import WindowsDNSImportError, parse_windows_dns_server

__all__ = [
    "CommitResult",
    "CommitZoneResult",
    "ConflictAction",
    "ImportedRecord",
    "ImportedSOA",
    "ImportedZone",
    "ImportPreview",
    "ImportSource",
    "ImportSourceError",
    "PowerDNSImportError",
    "WindowsDNSImportError",
    "ZoneConflict",
    "commit_import",
    "detect_conflicts",
    "parse_bind9_archive",
    "parse_powerdns_server",
    "parse_windows_dns_server",
    "test_powerdns_connection",
]

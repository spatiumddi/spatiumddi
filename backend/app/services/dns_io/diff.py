"""Diff parsed zone-file records against existing DNSRecord rows."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models.dns import DNSRecord
from app.services.dns_io.parser import ParsedRecord


@dataclass
class RecordChange:
    """A single record add / update / delete."""

    op: str  # "create" | "update" | "delete" | "unchanged"
    name: str
    record_type: str
    value: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None
    existing_id: str | None = None


@dataclass
class ZoneDiff:
    """Result of comparing parsed records to the existing zone."""

    to_create: list[RecordChange] = field(default_factory=list)
    to_update: list[RecordChange] = field(default_factory=list)
    to_delete: list[RecordChange] = field(default_factory=list)
    unchanged: list[RecordChange] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _record_key(name: str, rtype: str, value: str) -> tuple[str, str, str]:
    return (name.lower(), rtype.upper(), value)


def diff_records(
    parsed: Iterable[ParsedRecord],
    existing: Iterable[DNSRecord],
) -> ZoneDiff:
    """Compare ``parsed`` records against ``existing`` DB rows.

    Identity is (name, record_type, value). When that key matches but TTL
    or priority/weight/port differ, the change becomes an ``update``.
    """
    existing_by_key: dict[tuple[str, str, str], DNSRecord] = {
        _record_key(r.name, r.record_type, r.value): r for r in existing
    }
    diff = ZoneDiff()
    seen_keys: set[tuple[str, str, str]] = set()

    for p in parsed:
        key = _record_key(p.name, p.record_type, p.value)
        seen_keys.add(key)
        existing_row = existing_by_key.get(key)
        if existing_row is None:
            diff.to_create.append(
                RecordChange(
                    op="create",
                    name=p.name,
                    record_type=p.record_type,
                    value=p.value,
                    ttl=p.ttl,
                    priority=p.priority,
                    weight=p.weight,
                    port=p.port,
                )
            )
            continue

        ttl_changed = (p.ttl or 0) != (existing_row.ttl or 0)
        meta_changed = (
            p.priority != existing_row.priority
            or p.weight != existing_row.weight
            or p.port != existing_row.port
        )
        if ttl_changed or meta_changed:
            diff.to_update.append(
                RecordChange(
                    op="update",
                    name=p.name,
                    record_type=p.record_type,
                    value=p.value,
                    ttl=p.ttl,
                    priority=p.priority,
                    weight=p.weight,
                    port=p.port,
                    existing_id=str(existing_row.id),
                )
            )
        else:
            diff.unchanged.append(
                RecordChange(
                    op="unchanged",
                    name=p.name,
                    record_type=p.record_type,
                    value=p.value,
                    ttl=p.ttl,
                    priority=p.priority,
                    weight=p.weight,
                    port=p.port,
                    existing_id=str(existing_row.id),
                )
            )

    for key, row in existing_by_key.items():
        if key in seen_keys:
            continue
        diff.to_delete.append(
            RecordChange(
                op="delete",
                name=row.name,
                record_type=row.record_type,
                value=row.value,
                ttl=row.ttl,
                priority=row.priority,
                weight=row.weight,
                port=row.port,
                existing_id=str(row.id),
            )
        )

    return diff

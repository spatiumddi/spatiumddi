"""Format parsers for IPAM import payloads.

Input formats supported:
- CSV (first row is header; columns: network, name, gateway, vlan_id,
  description, space, block, plus any custom field columns).
- JSON (either a plain list of subnet objects, or an object with
  ``spaces``, ``blocks``, ``subnets``, ``addresses`` keys for a full
  hierarchical dump as produced by the exporter).
- XLSX (workbook with a ``subnets`` sheet at minimum; optional
  ``spaces``, ``blocks``, ``addresses`` sheets).

All parsers return a :class:`ParsedPayload` — a format-neutral intermediate
that the importer consumes.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status


@dataclass
class ParsedPayload:
    """Normalised import payload — lists of dict rows, one per resource type."""

    spaces: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)


_KNOWN_SUBNET_COLUMNS = {
    "network",
    "name",
    "gateway",
    "vlan_id",
    "vxlan_id",
    "description",
    "status",
    "domain_name",
    "space",
    "space_name",
    "block",
    "block_network",
}


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _row_to_subnet(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a generic row dict into a subnet dict with custom_fields."""
    out: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    for raw_key, value in row.items():
        if raw_key is None:
            continue
        key = raw_key.strip()
        if not key:
            continue
        norm = key.lower()
        if value == "":
            value = None
        if norm in _KNOWN_SUBNET_COLUMNS:
            out[norm] = value
        elif norm in {"vlan", "vlan id"}:
            out["vlan_id"] = value
        elif norm in {"vxlan", "vxlan id"}:
            out["vxlan_id"] = value
        else:
            # Everything unrecognised becomes a custom field.
            custom[key] = value
    if custom:
        out["custom_fields"] = custom
    # Normalise vlan_id
    if "vlan_id" in out:
        out["vlan_id"] = _coerce_int(out["vlan_id"])
    if "vxlan_id" in out:
        out["vxlan_id"] = _coerce_int(out["vxlan_id"])
    return out


def parse_csv(data: bytes) -> ParsedPayload:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    payload = ParsedPayload()
    for row in reader:
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        if not row.get("network") and not row.get("Network"):
            continue
        payload.subnets.append(_row_to_subnet(row))
    return payload


def parse_json(data: bytes) -> ParsedPayload:
    try:
        decoded = json.loads(data.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid JSON payload: {exc}",
        ) from exc

    payload = ParsedPayload()
    if isinstance(decoded, list):
        for item in decoded:
            if not isinstance(item, dict):
                continue
            payload.subnets.append(_row_to_subnet(item))
        return payload

    if isinstance(decoded, dict):
        for key in ("spaces", "blocks", "subnets", "addresses"):
            items = decoded.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if key == "subnets":
                    payload.subnets.append(_row_to_subnet(item))
                else:
                    getattr(payload, key).append(dict(item))
        return payload

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="JSON payload must be a list of subnets or an object with "
        "spaces/blocks/subnets/addresses keys",
    )


def parse_xlsx(data: bytes) -> ParsedPayload:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - runtime dep guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="openpyxl is required for XLSX import but is not installed",
        ) from exc

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid XLSX file: {exc}",
        ) from exc

    payload = ParsedPayload()

    def _sheet_rows(sheet_name: str) -> list[dict[str, Any]]:
        if sheet_name not in wb.sheetnames:
            return []
        ws = wb[sheet_name]
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return []
        headers = [str(h).strip() if h is not None else "" for h in header]
        out: list[dict[str, Any]] = []
        for raw in rows:
            if raw is None or all(c is None or c == "" for c in raw):
                continue
            row = {headers[i]: raw[i] for i in range(min(len(headers), len(raw))) if headers[i]}
            out.append(row)
        return out

    # The primary / single-sheet case: use the first sheet as subnets
    subnet_sheet = (
        "subnets"
        if "subnets" in wb.sheetnames
        else wb.sheetnames[0]
        if wb.sheetnames
        else None
    )
    if subnet_sheet:
        for row in _sheet_rows(subnet_sheet):
            if not row.get("network") and not row.get("Network"):
                continue
            payload.subnets.append(_row_to_subnet(row))

    for key in ("spaces", "blocks", "addresses"):
        for row in _sheet_rows(key):
            getattr(payload, key).append(dict(row))

    wb.close()
    return payload


def parse_payload(data: bytes, filename: str, content_type: str | None) -> ParsedPayload:
    """Dispatch to the format parser based on the file extension / MIME type."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".csv") or "csv" in ctype:
        return parse_csv(data)
    if name.endswith(".json") or "json" in ctype:
        return parse_json(data)
    if name.endswith(".xlsx") or "spreadsheetml" in ctype or "officedocument" in ctype:
        return parse_xlsx(data)
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=f"Unsupported import format: {filename or content_type or 'unknown'}. "
        "Use CSV, JSON, or XLSX.",
    )

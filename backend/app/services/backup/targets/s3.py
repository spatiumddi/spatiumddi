"""AWS S3 + S3-compatible backup destination (issue #117 Phase 1c).

Covers AWS S3 plus every S3-compatible service we care about
via the ``endpoint_url`` config field — MinIO, Wasabi, Backblaze
B2, Cloudflare R2, DigitalOcean Spaces, Linode Object Storage.
Operators leaving ``endpoint_url`` blank get the AWS endpoint
for the configured region.

Config shape:

* ``bucket`` — required.
* ``region`` — required even for S3-compatible (boto3 still
  needs one in the signing path; ``us-east-1`` is the safe
  default for non-AWS endpoints).
* ``prefix`` — optional key prefix (``"backups/"``) so multiple
  installs can share a bucket.
* ``endpoint_url`` — optional; populates for S3-compatible.
* ``access_key_id`` — required, plaintext.
* ``secret_access_key`` — required, **secret** (Fernet-wrapped
  in JSONB via :mod:`app.services.backup.targets.secrets_config`).
* ``addressing_style`` — optional (``"virtual"`` / ``"path"``).
  Defaults to virtual; some MinIO deploys + older bucket names
  with dots need ``"path"``.

Implementation notes:

* boto3 is sync; every driver method wraps the underlying
  client calls in :func:`asyncio.to_thread` so the asyncio
  event loop never blocks on a slow upstream.
* A fresh client is created per call. Per-call cost is minimal
  (no real HTTP traffic until the first operation), and the
  alternative (a per-target singleton) introduces lifecycle
  bugs we don't need.
* Probe / archive listings filter to the same regex as the
  local-volume driver — operators sharing a bucket with
  unrelated objects don't see them in our list.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from typing import Any

import structlog

from app.services.backup.targets.base import (
    ArchiveListing,
    BackupDestination,
    BackupDestinationError,
    ConfigFieldSpec,
    DestinationConfigError,
)

logger = structlog.get_logger(__name__)

# Same archive-name pattern as the local-volume driver.
_ARCHIVE_NAME_RE = re.compile(r"^(spatiumddi-backup-|pre-restore-).*\.zip$")

# boto3 ClientError import is lazy — keeps the import-graph cost off
# the hot path and avoids forcing every install to ship boto3 once
# we add destinations that don't need it.


def _client(config: dict[str, Any]):
    """Build a fresh boto3 S3 client. Lazy boto3 import keeps the
    backup module light for installs that don't use S3.
    """
    import boto3  # noqa: PLC0415
    from botocore.config import Config as BotoConfig  # noqa: PLC0415

    addressing_style = config.get("addressing_style") or "virtual"
    if addressing_style not in {"virtual", "path", "auto"}:
        raise DestinationConfigError(
            f"addressing_style must be 'virtual' / 'path' / 'auto' " f"(got {addressing_style!r})"
        )
    boto_cfg = BotoConfig(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
        s3={"addressing_style": addressing_style},
    )
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "aws_access_key_id": config["access_key_id"],
        "aws_secret_access_key": config["secret_access_key"],
        "region_name": config["region"],
        "config": boto_cfg,
    }
    if config.get("endpoint_url"):
        kwargs["endpoint_url"] = config["endpoint_url"]
    return boto3.client(**kwargs)


def _key(config: dict[str, Any], filename: str) -> str:
    """Compose the object key from the optional prefix + filename.
    ``os.path.basename`` defends against operator-typed paths in
    ``filename`` that try to escape the prefix.
    """
    prefix = (config.get("prefix") or "").strip("/")
    safe = os.path.basename(filename)
    if prefix:
        return f"{prefix}/{safe}"
    return safe


def _strip_prefix(config: dict[str, Any], key: str) -> str:
    prefix = (config.get("prefix") or "").strip("/")
    if prefix and key.startswith(prefix + "/"):
        return key[len(prefix) + 1 :]
    return key


class S3Destination(BackupDestination):
    kind = "s3"
    label = "AWS S3 / S3-compatible"
    config_fields = (
        ConfigFieldSpec(
            name="bucket",
            label="Bucket",
            type="text",
            required=True,
            description="Existing bucket name. The driver doesn't create the bucket itself.",
        ),
        ConfigFieldSpec(
            name="region",
            label="Region",
            type="text",
            required=True,
            description=(
                "e.g. us-east-1. Required even for S3-compatible — "
                "boto3 needs one for request signing."
            ),
        ),
        ConfigFieldSpec(
            name="prefix",
            label="Key prefix",
            type="text",
            required=False,
            description="Optional. e.g. spatiumddi/backups/ — useful when sharing a bucket.",
        ),
        ConfigFieldSpec(
            name="endpoint_url",
            label="Endpoint URL",
            type="text",
            required=False,
            description=(
                "Leave blank for AWS S3. Set for S3-compatible "
                "(MinIO, Wasabi, Backblaze B2, Cloudflare R2)."
            ),
        ),
        ConfigFieldSpec(
            name="addressing_style",
            label="Addressing style",
            type="text",
            required=False,
            description=("'virtual' (default), 'path' (MinIO + buckets " "with dots), or 'auto'."),
        ),
        ConfigFieldSpec(
            name="access_key_id",
            label="Access key ID",
            type="text",
            required=True,
        ),
        ConfigFieldSpec(
            name="secret_access_key",
            label="Secret access key",
            type="password",
            required=True,
            secret=True,
            description="Encrypted at rest. Leave the existing field unchanged on edit to keep the previous value.",
        ),
    )

    def validate_config(self, config: dict[str, Any]) -> None:
        for required in ("bucket", "region", "access_key_id", "secret_access_key"):
            value = config.get(required)
            if not value or not isinstance(value, str):
                raise DestinationConfigError(
                    f"{required!r} is required and must be a non-empty string"
                )
        endpoint = config.get("endpoint_url")
        if endpoint and not isinstance(endpoint, str):
            raise DestinationConfigError("'endpoint_url' must be a string")
        if endpoint and not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise DestinationConfigError("'endpoint_url' must start with http:// or https://")
        prefix = config.get("prefix")
        if prefix and not isinstance(prefix, str):
            raise DestinationConfigError("'prefix' must be a string")

    async def write(
        self,
        *,
        config: dict[str, Any],
        filename: str,
        archive_bytes: bytes,
    ) -> None:
        key = _key(config, filename)

        def _do() -> None:
            from botocore.exceptions import (  # noqa: PLC0415
                BotoCoreError,
                ClientError,
            )

            client = _client(config)
            try:
                client.put_object(
                    Bucket=config["bucket"],
                    Key=key,
                    Body=archive_bytes,
                    ContentType="application/zip",
                )
            except (ClientError, BotoCoreError) as exc:
                raise BackupDestinationError(f"S3 put_object failed: {exc}") from exc

        await asyncio.to_thread(_do)

    async def list_archives(self, *, config: dict[str, Any]) -> list[ArchiveListing]:
        prefix = (config.get("prefix") or "").strip("/")
        list_prefix = f"{prefix}/" if prefix else ""

        def _do() -> list[ArchiveListing]:
            from botocore.exceptions import (  # noqa: PLC0415
                BotoCoreError,
                ClientError,
            )

            client = _client(config)
            rows: list[ArchiveListing] = []
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=config["bucket"], Prefix=list_prefix):
                    for obj in page.get("Contents") or []:
                        key = obj["Key"]
                        filename = _strip_prefix(config, key)
                        if not _ARCHIVE_NAME_RE.match(filename):
                            continue
                        last_modified = obj.get("LastModified")
                        if last_modified is None:
                            continue
                        if last_modified.tzinfo is None:
                            last_modified = last_modified.replace(tzinfo=UTC)
                        rows.append(
                            ArchiveListing(
                                filename=filename,
                                size_bytes=int(obj.get("Size") or 0),
                                created_at=last_modified.astimezone(UTC),
                            )
                        )
            except (ClientError, BotoCoreError) as exc:
                raise BackupDestinationError(f"S3 list_objects_v2 failed: {exc}") from exc
            rows.sort(key=lambda r: r.created_at, reverse=True)
            return rows

        return await asyncio.to_thread(_do)

    async def delete(self, *, config: dict[str, Any], filename: str) -> None:
        key = _key(config, filename)

        def _do() -> None:
            from botocore.exceptions import (  # noqa: PLC0415
                BotoCoreError,
                ClientError,
            )

            client = _client(config)
            try:
                client.delete_object(Bucket=config["bucket"], Key=key)
            except (ClientError, BotoCoreError) as exc:
                raise BackupDestinationError(f"S3 delete_object failed: {exc}") from exc

        await asyncio.to_thread(_do)

    async def test_connection(self, *, config: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate_config(config)
        except DestinationConfigError as exc:
            return {"ok": False, "error": str(exc)}
        probe_name = f"spatiumddi-test-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.bin"
        probe_key = _key(config, probe_name)

        def _do() -> dict[str, Any]:
            from botocore.exceptions import (  # noqa: PLC0415
                BotoCoreError,
                ClientError,
            )

            client = _client(config)
            try:
                client.put_object(
                    Bucket=config["bucket"],
                    Key=probe_key,
                    Body=os.urandom(16),
                    ContentType="application/octet-stream",
                )
                head = client.head_object(Bucket=config["bucket"], Key=probe_key)
                ok = head.get("ContentLength", 0) == 16
                client.delete_object(Bucket=config["bucket"], Key=probe_key)
            except (ClientError, BotoCoreError) as exc:
                # Distinguish auth errors from missing-bucket so the
                # operator gets a useful nudge.
                msg = str(exc)
                if "InvalidAccessKeyId" in msg or "SignatureDoesNotMatch" in msg:
                    return {"ok": False, "error": f"auth failed: {msg}"}
                if "NoSuchBucket" in msg or "404" in msg:
                    return {
                        "ok": False,
                        "error": f"bucket not found or no access: {msg}",
                    }
                return {"ok": False, "error": msg}
            if not ok:
                return {
                    "ok": False,
                    "error": "wrote probe but head_object disagreed on size",
                }
            return {
                "ok": True,
                "detail": (
                    f"wrote + verified + deleted probe at " f"{config['bucket']}/{probe_key}"
                ),
            }

        try:
            return await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"unexpected: {exc}"}

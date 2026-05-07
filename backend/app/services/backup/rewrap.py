"""Cross-install secret rewrap for restore (issue #117 Phase 2).

When an operator restores an archive on an install whose
``SECRET_KEY`` differs from the one in ``secrets.enc``, every
Fernet-encrypted column in the database still carries the source
install's ciphertext — readable only with the source key, which
isn't on disk anywhere. Without rewrap, the operator has to
manually copy the recovered ``SECRET_KEY`` into the destination's
``.env`` and restart everything; otherwise integration creds /
auth-provider secrets / TSIG keys / etc. all fail to decrypt.

This module runs after the data-replay phase of restore. It:

1. Builds two ``Fernet`` instances using the same derivation
   logic as :mod:`app.core.crypto` — one from the source install's
   key (recovered from ``secrets.enc``), one from the local
   install's key.
2. Bails early when the keys are identical (same-install
   restore — no rewrap needed).
3. For every (table, column) in :data:`ENCRYPTED_COLUMNS`,
   walks every non-null row, decrypts with the source key,
   re-encrypts with the destination key, UPDATEs the row.
4. Walks every ``__enc__:`` field inside ``backup_target.config``
   — those are JSONB-embedded Fernet strings, separate from the
   column-level table.

Idempotency: if a row is already encrypted with the destination
key (rewrap re-run, or a row created post-restore via the API),
``InvalidToken`` from the source-key attempt is swallowed and we
verify against the dest key — already-rewrapped rows count as
``skipped_idempotent``, not failures. Rows that decrypt with
neither key are logged as ``failed`` and left untouched; the
restore still succeeds, the operator gets a count.

The rewrap opens its own asyncpg connection rather than reusing
the SQLAlchemy engine — the restore phase disposes that engine
on purpose (so psql / pg_restore can run unobstructed), and this
runs immediately after that disposal. A short-lived asyncpg
connection avoids reviving the pool prematurely.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import structlog
from cryptography.fernet import Fernet, InvalidToken

logger = structlog.get_logger(__name__)

# All (table, primary_key_column, encrypted_bytes_column) triples
# in the schema. Sorted alphabetically by table for diffability.
# When a new model adds a Fernet-encrypted ``LargeBinary`` column,
# extend this list — there's no auto-discovery, on purpose, so a
# rewrap-affecting schema change is an explicit code review.
ENCRYPTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("ai_provider", "id", "api_key_encrypted"),
    ("audit_forward_target", "id", "smtp_password_encrypted"),
    ("auth_provider", "id", "secrets_encrypted"),
    ("backup_target", "id", "passphrase_encrypted"),
    ("dhcp_server", "id", "credentials_encrypted"),
    ("dns_server", "id", "credentials_encrypted"),
    ("dns_tsig_key", "id", "secret_encrypted"),
    ("docker_host", "id", "client_key_encrypted"),
    ("event_subscription", "id", "secret_encrypted"),
    ("kubernetes_cluster", "id", "token_encrypted"),
    ("network_device", "id", "community_encrypted"),
    ("network_device", "id", "v3_auth_key_encrypted"),
    ("network_device", "id", "v3_priv_key_encrypted"),
    ("platform_settings", "id", "fingerbank_api_key_encrypted"),
    ("proxmox_node", "id", "token_secret_encrypted"),
    ("tailscale_target", "id", "api_key_encrypted"),
    ("unifi_controller", "id", "api_key_encrypted"),
    ("unifi_controller", "id", "username_encrypted"),
    ("unifi_controller", "id", "password_encrypted"),
    ('"user"', "id", "totp_secret_encrypted"),
    ('"user"', "id", "recovery_codes_encrypted"),
    ('"user"', "id", "password_history_encrypted"),
)

# The same prefix the ``backup_target.config`` JSONB serializer
# stamps on every Fernet-wrapped field (see
# :mod:`app.services.backup.targets.secrets_config`). Anything
# else inside the config blob is plaintext and stays put.
_ENC_PREFIX = "__enc__:"


@dataclass
class RewrapOutcome:
    """Per-restore counters surfaced to the operator + audit row."""

    same_install: bool = False
    rewrapped_rows: int = 0
    skipped_idempotent_rows: int = 0
    failed_rows: int = 0
    rewrapped_jsonb_fields: int = 0
    columns_visited: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)


def _fernet_from_keys(secret_key: str, credential_encryption_key: str) -> Fernet:
    """Build a Fernet using the same precedence rules as
    :func:`app.core.crypto._fernet`. ``credential_encryption_key``
    wins when set + parseable; otherwise SHA-256 over
    ``secret_key`` produces the 32-byte key material.

    Raises :class:`ValueError` only when *neither* path yields a
    usable key — in practice the SHA-256 fallback always works.
    """
    raw = (credential_encryption_key or "").strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except (ValueError, Exception):  # noqa: BLE001
            # Mirrors crypto.py: malformed explicit key → fall back
            # to the derived key so the install still functions.
            pass
    digest = hashlib.sha256((secret_key or "").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _keys_identical(
    source_secret: str,
    source_cred: str,
    dest_secret: str,
    dest_cred: str,
) -> bool:
    """Return True when the source + destination Fernet keys would
    produce identical decryption results. We compare the *derived*
    32-byte key material — the explicit-credential-key path takes
    precedence over the derived-from-secret_key path in both
    crypto.py + _fernet_from_keys above, so the comparison must
    match that precedence.
    """
    source = (source_cred or "").strip() or _derived_b64(source_secret)
    dest = (dest_cred or "").strip() or _derived_b64(dest_secret)
    return source == dest


def _derived_b64(secret_key: str) -> str:
    digest = hashlib.sha256((secret_key or "").encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def _rewrap_value(source: Fernet, dest: Fernet, ciphertext: bytes) -> tuple[bytes | None, str]:
    """Decrypt ``ciphertext`` with the source key and re-encrypt
    with the destination key. Returns:

    * ``(new_ciphertext, "rewrapped")`` on success.
    * ``(None, "idempotent")`` when the source key fails but the
      destination key already decrypts the value — this row is
      already rewrapped (a re-run, or it was created post-restore).
    * ``(None, "failed:<short reason>")`` when neither key works —
      the row is left untouched and the failure is counted.
    """
    try:
        plaintext = source.decrypt(ciphertext)
    except InvalidToken:
        # Maybe already rewrapped — confirm by trying the dest key.
        try:
            dest.decrypt(ciphertext)
            return None, "idempotent"
        except InvalidToken:
            return None, "failed:not-decryptable-with-either-key"
    try:
        return dest.encrypt(plaintext), "rewrapped"
    except Exception as exc:  # noqa: BLE001
        return None, f"failed:re-encrypt:{exc}"


async def rewrap_secrets(
    *,
    db_url: str,
    source_secret_key: str,
    source_credential_key: str,
    dest_secret_key: str,
    dest_credential_key: str,
) -> RewrapOutcome:
    """Walk every Fernet-encrypted column + the
    ``backup_target.config`` JSONB blob, rewrap each value from
    ``source`` to ``dest`` keys.

    No-ops cleanly when the source + destination keys are identical
    (same-install restore — counters return zero, ``same_install``
    flag set).
    """
    outcome = RewrapOutcome()
    if _keys_identical(
        source_secret_key,
        source_credential_key,
        dest_secret_key,
        dest_credential_key,
    ):
        outcome.same_install = True
        return outcome

    source_fernet = _fernet_from_keys(source_secret_key, source_credential_key)
    dest_fernet = _fernet_from_keys(dest_secret_key, dest_credential_key)

    # asyncpg uses ``postgresql://`` (or ``postgres://``) URLs —
    # SQLAlchemy's ``postgresql+asyncpg://`` is unparseable. Strip
    # the dialect suffix.
    pg_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    conn = await asyncpg.connect(dsn=pg_url)
    try:
        for table, pk_col, enc_col in ENCRYPTED_COLUMNS:
            outcome.columns_visited += 1
            await _rewrap_column(
                conn=conn,
                table=table,
                pk_col=pk_col,
                enc_col=enc_col,
                source=source_fernet,
                dest=dest_fernet,
                outcome=outcome,
            )
        # Walk backup_target.config JSONB last — its column was
        # already covered above for the row's passphrase, but the
        # config blob carries its own per-driver Fernet-wrapped
        # secrets (S3 secret_access_key, SCP private_key, Azure
        # account_key, etc.).
        await _rewrap_backup_target_config(
            conn=conn,
            source=source_fernet,
            dest=dest_fernet,
            outcome=outcome,
        )
    finally:
        await conn.close()

    return outcome


async def _rewrap_column(
    *,
    conn: asyncpg.Connection,
    table: str,
    pk_col: str,
    enc_col: str,
    source: Fernet,
    dest: Fernet,
    outcome: RewrapOutcome,
) -> None:
    """Rewrap every non-null value in one column. The whole
    column's batch runs inside a transaction so a mid-table crash
    leaves a consistent set of rewrapped + untouched rows rather
    than a half-rewrapped table.
    """
    # ``length`` filter skips the empty-bytes default columns
    # (Docker / Tailscale / UniFi / Proxmox carry empty bytea
    # defaults instead of NULL for newly-added rows).
    query = (
        f"SELECT {pk_col}, {enc_col} FROM {table} "
        f"WHERE {enc_col} IS NOT NULL AND length({enc_col}) > 0"
    )
    try:
        rows = await conn.fetch(query)
    except asyncpg.UndefinedTableError:
        # Table doesn't exist on this install — possible if the
        # operator restored from a feature module they don't have
        # enabled. Skip cleanly.
        logger.info("rewrap_table_missing", table=table, column=enc_col)
        return
    except asyncpg.UndefinedColumnError:
        # Column doesn't exist on this install — same reason.
        logger.info("rewrap_column_missing", table=table, column=enc_col)
        return

    if not rows:
        return

    async with conn.transaction():
        for row in rows:
            pk = row[pk_col.strip('"')]
            ciphertext = bytes(row[enc_col])
            new_value, status = _rewrap_value(source, dest, ciphertext)
            if status == "rewrapped":
                await conn.execute(
                    f"UPDATE {table} SET {enc_col} = $1 WHERE {pk_col} = $2",
                    new_value,
                    pk,
                )
                outcome.rewrapped_rows += 1
            elif status == "idempotent":
                outcome.skipped_idempotent_rows += 1
            else:
                outcome.failed_rows += 1
                # Track the first ten so the audit row has a
                # debuggable shape without ballooning to thousands
                # of entries on a pathological install.
                if len(outcome.failures) < 10:
                    outcome.failures.append(
                        {
                            "table": table.strip('"'),
                            "column": enc_col,
                            "pk": str(pk),
                            "reason": status,
                        }
                    )
                logger.warning(
                    "rewrap_row_failed",
                    table=table.strip('"'),
                    column=enc_col,
                    pk=str(pk),
                    reason=status,
                )


async def _rewrap_backup_target_config(
    *,
    conn: asyncpg.Connection,
    source: Fernet,
    dest: Fernet,
    outcome: RewrapOutcome,
) -> None:
    """Rewrap every ``__enc__:``-prefixed string inside the
    ``backup_target.config`` JSONB blob. Each driver embeds
    secrets there at row-create time (see
    :mod:`app.services.backup.targets.secrets_config`) so the
    column-level rewrap above only handles ``passphrase_encrypted``;
    the driver-side credentials live here.

    JSONB rows are read, mutated in memory, then UPDATEd as a
    whole — there's no UPDATE-by-jsonb-key shortcut that would
    play nicely with multi-field rewrap.
    """
    try:
        rows = await conn.fetch("SELECT id, config FROM backup_target")
    except asyncpg.UndefinedTableError:
        return

    if not rows:
        return

    async with conn.transaction():
        for row in rows:
            target_id = row["id"]
            config_raw = row["config"]
            if config_raw is None:
                continue
            # asyncpg returns JSONB as already-decoded Python types;
            # if a future driver schema returns it as raw bytes we
            # still defend with json.loads.
            config: dict[str, Any]
            if isinstance(config_raw, str):
                config = json.loads(config_raw)
            elif isinstance(config_raw, bytes):
                config = json.loads(config_raw.decode("utf-8"))
            else:
                config = dict(config_raw)
            mutated = False
            for key, value in list(config.items()):
                if not isinstance(value, str) or not value.startswith(_ENC_PREFIX):
                    continue
                ciphertext = value[len(_ENC_PREFIX) :].encode("utf-8")
                new_value, status = _rewrap_value(source, dest, ciphertext)
                if status == "rewrapped":
                    config[key] = _ENC_PREFIX + new_value.decode("utf-8")
                    mutated = True
                    outcome.rewrapped_jsonb_fields += 1
                elif status == "idempotent":
                    outcome.skipped_idempotent_rows += 1
                else:
                    outcome.failed_rows += 1
                    if len(outcome.failures) < 10:
                        outcome.failures.append(
                            {
                                "table": "backup_target",
                                "column": f"config.{key}",
                                "pk": str(target_id),
                                "reason": status,
                            }
                        )
                    logger.warning(
                        "rewrap_jsonb_field_failed",
                        target_id=str(target_id),
                        field=key,
                        reason=status,
                    )
            if mutated:
                await conn.execute(
                    "UPDATE backup_target SET config = $1::jsonb WHERE id = $2",
                    json.dumps(config),
                    target_id,
                )

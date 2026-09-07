"""Generic HTTPS ``PUT`` / ``POST`` backup destination (issue #989 item 2).

Artifactory and Nexus generic repositories, a presigned S3 URL, an
internal receiver somebody wrote — all take a body with a bearer or
basic credential, and none of them fit WebDAV, which needs ``PROPFIND``
and a collection to enumerate. ``httpx`` is already the WebDAV driver's
transport, so this is a small driver.

**It is inherently write-only**, which is why it ships alongside item 1
rather than before it. There is no listing verb to enumerate archives
with and no delete verb to prune with — so ``inherently_write_only`` is
set, and the API forces ``backup_target.write_only`` on for this kind.
Without that an operator could configure ``retention_keep_last_n`` on a
destination that can never prune, and the failure would be silent and
nightly.

That constraint has real consequences an operator should decide about up
front, so the form says them rather than leaving them to be discovered:

* **No restore-from-destination and no restore drill.** Both start from
  a listing. The drill reports ``cannot_drill_write_only`` and readiness
  counts the target as UNVERIFIED — never as healthy — because an
  unverifiable backup is an unknown, not a pass.
* **No pull mode.** ``GET .../archives/latest/download`` needs a
  listing too; it answers 409 naming this kind.
* **Retention is the receiver's job.** Artifactory cleanup policies, a
  Nexus task, an S3 lifecycle rule.

Pair it with a second, readable destination if the archives need to be
verifiable — which they do.

Config shape
------------

* ``url`` — the receiver. May contain a ``{filename}`` placeholder,
  which is substituted with the archive name; without one the archive
  name is appended as a path segment. A presigned URL is used verbatim
  (no placeholder, nothing appended) — the form flags that presigned
  URLs expire, which makes them a poor fit for a *scheduled* backup.
* ``method`` — ``PUT`` (default) or ``POST``.
* ``auth`` — ``none`` / ``bearer`` / ``basic`` / ``header``.
* ``username`` — for ``basic``.
* ``credential`` — the token / password / header value. **Secret**,
  Fernet-wrapped at rest like every other kind.
* ``header_name`` — for ``auth=header`` (e.g. ``X-JFrog-Art-Api``).
* ``extra_headers`` — optional ``Name: value`` lines.
* ``verify_tls`` — ``"true"`` (default).

The body is the raw archive with ``Content-Type: application/zip`` and
``Content-Length``. No multipart: the receivers that want multipart are
the ones WebDAV or S3 already cover.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import structlog

from app.core.ssrf import SSRFBlockedError, assert_safe_target
from app.services.backup.targets.base import (
    ArchiveListing,
    BackupDestination,
    BackupDestinationError,
    ConfigFieldSpec,
    DestinationConfigError,
    UnsupportedOperationError,
)

logger = structlog.get_logger(__name__)

#: Generous, matching the WebDAV driver: a multi-GB archive over a slow
#: link legitimately takes a long time.
_TIMEOUT = httpx.Timeout(300.0, connect=15.0)

_FILENAME_TOKEN = "{filename}"

_AUTH_MODES = ("none", "bearer", "basic", "header")


def _safe_filename(filename: str) -> str:
    return os.path.basename(filename)


def _target_url(config: dict[str, Any], filename: str) -> str:
    """Compose the URL for one archive.

    Three shapes, in priority order:

    1. ``{filename}`` placeholder — substituted (percent-encoded, since
       it lands in a path segment).
    2. URL with a query string (a presigned URL) — used **verbatim**.
       Appending a path segment to a presigned URL invalidates its
       signature, so doing that would break every presigned target with
       an error from the far end that says nothing useful.
    3. Otherwise — the archive name is appended as a path segment.
    """
    url = config["url"]
    name = _safe_filename(filename)
    if _FILENAME_TOKEN in url:
        return url.replace(_FILENAME_TOKEN, quote(name, safe=""))
    if urlsplit(url).query:
        return url
    return url.rstrip("/") + "/" + quote(name, safe="")


def _parse_extra_headers(config: dict[str, Any]) -> dict[str, str]:
    raw = (config.get("extra_headers") or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise DestinationConfigError(f"'extra_headers' line {line!r} is not 'Name: value'")
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        if not name:
            raise DestinationConfigError("'extra_headers' has a line with an empty name")
        # A newline smuggled through a header value is header injection;
        # ``str.splitlines`` has already split on every newline form, so
        # reaching this with one would mean a bug above.
        if any(c in value for c in "\r\n"):
            raise DestinationConfigError(f"'extra_headers' value for {name!r} contains a newline")
        out[name] = value
    return out


def _auth_mode(config: dict[str, Any]) -> str:
    mode = (config.get("auth") or "none").strip().lower()
    if mode not in _AUTH_MODES:
        raise DestinationConfigError(f"'auth' must be one of {list(_AUTH_MODES)} (got {mode!r})")
    return mode


def _method(config: dict[str, Any]) -> str:
    method = (config.get("method") or "PUT").strip().upper()
    if method not in ("PUT", "POST"):
        raise DestinationConfigError(f"'method' must be PUT or POST (got {method!r})")
    return method


class HttpsPutDestination(BackupDestination):
    kind = "https_put"
    label = "HTTPS PUT / POST (Artifactory, Nexus, presigned URL)"
    inherently_write_only = True
    config_fields = (
        ConfigFieldSpec(
            name="_write_only_notice",
            label="This destination is write-only",
            type="notice",
            required=False,
            description=(
                "A plain HTTP receiver has no way to list or delete what it has "
                "been sent, so this kind cannot prune, cannot serve pull-mode "
                "downloads, and cannot be restore-drilled — its recovery readiness "
                "reads UNVERIFIED, not healthy. Retention is the receiver's job "
                "(an Artifactory cleanup policy, a Nexus task, an S3 lifecycle "
                "rule). Pair it with a readable destination if the archives need "
                "to be verifiable."
            ),
        ),
        ConfigFieldSpec(
            name="url",
            label="Receiver URL",
            type="text",
            required=True,
            description=(
                "Where each archive is sent. Include {filename} to place the archive "
                "name (e.g. https://nexus.example/repository/backups/{filename}); "
                "otherwise it is appended as a path segment. A presigned URL is used "
                "exactly as given — note that presigned URLs expire, which makes them "
                "a poor fit for a recurring schedule."
            ),
        ),
        ConfigFieldSpec(
            name="method",
            label="Method",
            type="text",
            required=False,
            description="'PUT' (default) or 'POST'.",
        ),
        ConfigFieldSpec(
            name="auth",
            label="Authentication",
            type="text",
            required=False,
            description=(
                "'none' (default — for a presigned URL), 'bearer', 'basic', or "
                "'header' for a vendor API-key header."
            ),
        ),
        ConfigFieldSpec(
            name="username",
            label="Username",
            type="text",
            required=False,
            description="Only used when auth is 'basic'.",
        ),
        ConfigFieldSpec(
            name="credential",
            label="Token / password / header value",
            type="password",
            required=False,
            secret=True,
            description=(
                "Encrypted at rest. Leave unchanged on edit to keep the previous "
                "value. Not needed when auth is 'none'."
            ),
        ),
        ConfigFieldSpec(
            name="header_name",
            label="Header name",
            type="text",
            required=False,
            description="Only used when auth is 'header'. e.g. X-JFrog-Art-Api.",
        ),
        ConfigFieldSpec(
            name="extra_headers",
            label="Extra headers",
            type="text",
            required=False,
            description="Optional, one 'Name: value' per line.",
        ),
        ConfigFieldSpec(
            name="verify_tls",
            label="Verify TLS certificate",
            type="text",
            required=False,
            description="'true' (default). Set 'false' only for self-signed labs.",
        ),
    )

    # ── validation ────────────────────────────────────────────────────

    def validate_config(self, config: dict[str, Any]) -> None:
        url = config.get("url")
        if not url or not isinstance(url, str):
            raise DestinationConfigError("'url' is required and must be a non-empty string")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise DestinationConfigError("'url' must start with http:// or https://")
        _method(config)
        mode = _auth_mode(config)
        _parse_extra_headers(config)
        if mode != "none" and not config.get("credential"):
            raise DestinationConfigError(f"'credential' is required when auth is {mode!r}")
        if mode == "basic" and not config.get("username"):
            raise DestinationConfigError("'username' is required when auth is 'basic'")
        if mode == "header" and not config.get("header_name"):
            raise DestinationConfigError("'header_name' is required when auth is 'header'")

    async def validate_config_network(self, config: dict[str, Any]) -> None:
        """Resolve the URL and refuse an SSRF pivot.

        ``block=True`` — stricter than the advisory default most callers
        use — because this destination carries the install's entire
        database off-box under an operator-supplied URL. A URL resolving
        to loopback, link-local or the cloud metadata endpoint is refused
        rather than logged.

        Runs here, not in :meth:`validate_config`, so the DNS lookup
        happens at create / update / test and never on the scheduled-run
        path. ``asyncio.to_thread`` because ``assert_safe_target`` calls
        ``getaddrinfo`` synchronously.
        """
        url = config.get("url")
        if not url or not isinstance(url, str):
            return  # the structural pass already rejected this
        try:
            await asyncio.to_thread(assert_safe_target, url, label="backup_https_put", block=True)
        except SSRFBlockedError as exc:
            raise DestinationConfigError(f"refusing this URL: {exc}") from exc

    # ── request plumbing ──────────────────────────────────────────────

    def _client(self, config: dict[str, Any]) -> httpx.AsyncClient:
        verify = (config.get("verify_tls") or "true").strip().lower() != "false"
        auth = None
        if _auth_mode(config) == "basic":
            auth = (config.get("username") or "", config.get("credential") or "")
        return httpx.AsyncClient(
            auth=auth,
            timeout=_TIMEOUT,
            verify=verify,
            # A redirect would re-send the archive AND its credential to
            # wherever the far end points, which is an SSRF the guard on
            # the configured URL cannot see.
            follow_redirects=False,
        )

    def _headers(self, config: dict[str, Any], *, content_type: str) -> dict[str, str]:
        headers = {"Content-Type": content_type}
        mode = _auth_mode(config)
        credential = config.get("credential") or ""
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {credential}"
        elif mode == "header":
            headers[str(config["header_name"]).strip()] = credential
        headers.update(_parse_extra_headers(config))
        return headers

    async def _send(
        self,
        *,
        config: dict[str, Any],
        url: str,
        body: bytes,
        content_type: str,
    ) -> httpx.Response:
        method = _method(config)
        async with self._client(config) as client:
            try:
                return await client.request(
                    method,
                    url,
                    content=body,
                    headers={
                        **self._headers(config, content_type=content_type),
                        "Content-Length": str(len(body)),
                    },
                )
            except httpx.HTTPError as exc:
                raise BackupDestinationError(f"{method} to {url} failed: {exc}") from exc

    # ── operations ────────────────────────────────────────────────────

    async def write(
        self,
        *,
        config: dict[str, Any],
        filename: str,
        archive_bytes: bytes,
    ) -> None:
        url = _target_url(config, filename)
        resp = await self._send(
            config=config, url=url, body=archive_bytes, content_type="application/zip"
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            raise BackupDestinationError(
                f"receiver redirected to {resp.headers.get('location', '?')!r}. Redirects "
                "are not followed here, because that would re-send the archive and its "
                "credential to an address the SSRF guard never checked — point the URL "
                "at the final location instead."
            )
        if resp.status_code // 100 != 2:
            raise BackupDestinationError(
                f"{_method(config)} to {url} returned {resp.status_code}: {resp.text[:300]}"
            )

    async def list_archives(self, *, config: dict[str, Any]) -> list[ArchiveListing]:
        """Always empty — an HTTP receiver has no enumeration verb.

        Returning ``[]`` rather than raising is deliberate. Callers use
        an empty listing to mean "nothing to prune / nothing to offer",
        which is the correct behaviour here; raising would turn every
        scheduled run's retention step into a failed run. The places
        where "no listing" genuinely changes the answer — the restore
        drill and pull-mode download — check ``write_only`` and say so
        explicitly instead of inferring it from emptiness.
        """
        return []

    async def download(self, *, config: dict[str, Any], filename: str) -> bytes:
        raise UnsupportedOperationError(
            "the https_put destination is one-way: it can send an archive to the "
            "receiver but cannot read one back. Restore from the receiver's own "
            "interface, or keep a second, readable destination."
        )

    async def delete(self, *, config: dict[str, Any], filename: str) -> None:
        raise UnsupportedOperationError(
            "the https_put destination cannot delete — retention is the receiver's "
            "own policy (an Artifactory cleanup rule, a Nexus task, an S3 lifecycle "
            "rule)."
        )

    # ── probe ─────────────────────────────────────────────────────────

    async def test_connection(self, *, config: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate_config(config)
            await self.validate_config_network(config)
        except DestinationConfigError as exc:
            return {"ok": False, "error": str(exc)}

        probe_name = "spatiumddi-test-probe.bin"
        url = _target_url(config, probe_name)
        try:
            resp = await self._send(
                config=config,
                url=url,
                body=os.urandom(16),
                content_type="application/octet-stream",
            )
        except BackupDestinationError as exc:
            return {"ok": False, "error": str(exc)}

        method = _method(config)
        if resp.status_code == 405:
            # The most common misconfiguration by a distance, and the one
            # a bare "405" reads as a network fault. Name the fix.
            other = "POST" if method == "PUT" else "PUT"
            return {
                "ok": False,
                "error": (
                    f"the receiver does not accept {method} at this URL (405). "
                    f"Try method={other} — Nexus raw repositories take PUT, some "
                    "internal receivers only take POST."
                ),
            }
        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "error": (
                    f"authentication failed ({resp.status_code}) — check the auth mode "
                    f"and credential. Response: {resp.text[:200]}"
                ),
            }
        if resp.status_code in (301, 302, 303, 307, 308):
            return {
                "ok": False,
                "error": (
                    f"the receiver redirected to {resp.headers.get('location', '?')!r}. "
                    "Redirects are not followed (that would re-send the archive and its "
                    "credential to an unchecked address) — configure the final URL."
                ),
            }
        if resp.status_code // 100 != 2:
            return {
                "ok": False,
                "error": f"{method} returned {resp.status_code}: {resp.text[:200]}",
            }
        return {
            "ok": True,
            # There is no delete verb, so the probe object stays. Say so
            # rather than letting the operator find a stray file later
            # and wonder what wrote it.
            "probe_retained": True,
            "detail": (
                f"{method} to {url} returned {resp.status_code}. This destination cannot "
                f"delete, so the 16-byte probe object {probe_name!r} is left on the "
                "receiver — remove it there if it is in the way."
            ),
        }

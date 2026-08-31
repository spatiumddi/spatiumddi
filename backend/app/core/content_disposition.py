"""Safe ``Content-Disposition`` header construction.

Every ``attachment`` download in the API hands an operator-influenced
name to a header that Starlette encodes as **latin-1**, so building one
with an f-string is a bug in three separate ways, all of which we have
shipped at least once:

* a non-latin-1 character (a zone named ``9ᓴ``) raises
  ``UnicodeEncodeError`` inside ``Response.init_headers`` — a 500 on an
  already-rendered body;
* a bare ``CR`` / ``LF`` reaches the ASGI server, which refuses it
  (``RuntimeError: Invalid HTTP header value.`` out of uvicorn's
  httptools writer) and drops the connection with no response at all —
  latin-1-encodable, so the UnicodeEncodeError fix alone does not catch
  it;
* a ``"`` closes the quoted-string early, so ``x"; filename="evil.pdf``
  injects a second parameter into a header that still returns 200.

The fix is an allowlist on the ``filename`` parameter, never a
blocklist: ``.encode("ascii", "ignore")`` strips the first case and
leaves the other two. The original name survives in the RFC 6266
``filename*`` form, which is percent-encoded and therefore safe by
construction — that is where a client that understands it reads the
unicode name from.

``app.services.ipam_io.pdf._slugify`` delegates to
:func:`slugify_filename_part`; it was the first copy of this rule and
the reason this module exists rather than a fourth one.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["content_disposition", "slugify_filename_part"]

# Characters allowed through verbatim. Deliberately narrow: this lands
# in a quoted-string, so anything that could close it or terminate the
# header must not survive, and "which ASCII punctuation is safe in a
# quoted-string" is not a question worth re-answering per call site.
_SAFE_CHARS = "-_."


def _is_safe(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in _SAFE_CHARS)


def _fold(value: str) -> str:
    """Map every unsafe character to ``-`` and collapse the runs.

    Collapsing matters for legibility, not safety: without it
    ``"AV multicast (demo)"`` becomes ``AV-multicast--demo-`` and a name
    with any non-ASCII in it turns into a row of dashes.
    """
    folded = "".join(ch if _is_safe(ch) else "-" for ch in value)
    while "--" in folded:
        folded = folded.replace("--", "-")
    return folded


def slugify_filename_part(value: str, *, max_len: int = 60, fallback: str = "export") -> str:
    """Reduce an operator-supplied name to a safe filename fragment.

    Returns *fallback* when nothing survives (a name that is entirely
    non-ASCII, or empty).
    """
    return _fold(value).strip("-.")[:max_len] or fallback


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """Build a ``Content-Disposition`` value that is safe to send.

    Emits both forms RFC 6266 §4.3 recommends: a sanitised ASCII
    ``filename`` for clients that read only that, and ``filename*`` in
    UTF-8 carrying *filename* verbatim. ``quote(..., safe="")`` because
    the ext-value grammar (RFC 5987 §3.2) has no exception for ``/``,
    which ``quote`` leaves alone by default.
    """
    ascii_name = _fold(filename).strip("-") or "download"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"

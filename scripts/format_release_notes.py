#!/usr/bin/env python3
"""Reformat a CHANGELOG.md section for the GitHub release body.

CHANGELOG.md is hard-wrapped at ~70 chars for terminal reading. The
GitHub release renderer applies ``breaks: true`` GFM, which turns
every single ``\\n`` into a literal ``<br>`` — so a 54-line summary
paragraph reads as a tall narrow column instead of flowing prose.

This script reads the changelog section text on stdin and writes a
release-body-friendly version on stdout that:

1. Joins consecutive prose lines into a single line (so the renderer
   reflows them as one paragraph). Blank lines remain — they're the
   real paragraph break.
2. Leaves headings (``# / ## / ### / ####``), list items
   (``- ``, ``* ``, ``\\d+. ``), and fenced code blocks alone.
3. Renames the standard Keep-a-Changelog section headings with
   emoji prefixes for readability on GitHub.
4. Wraps the top prose paragraph (the release summary) in a new
   ``### 🚀 Highlights`` heading so it's visually distinct from the
   detail bullets below.

The transform is idempotent — re-running on already-transformed
input is a no-op (the emoji-prefixed headings don't double-prefix,
and prose paragraphs that are already a single line stay one line).

Usage::

    awk '/^## 2026\\.05\\.05-2/,/^## /' CHANGELOG.md \\
        | python3 scripts/format_release_notes.py
"""

from __future__ import annotations

import re
import sys

# ── Section heading rewrites ─────────────────────────────────────────
# Mirror Keep-a-Changelog's vocabulary (Added / Changed / Fixed /
# Removed / Deprecated / Security) plus our extras (Migrations).
# Match only at the start of the line and only when the heading
# isn't already emoji-prefixed (idempotency).
_SECTION_EMOJI: dict[str, str] = {
    "Added": "✨",
    "Changed": "🔧",
    "Fixed": "🐛",
    "Removed": "🗑️",
    "Deprecated": "⚠️",
    "Security": "🔒",
    "Migrations": "🗃️",
    "Breaking": "💥",
}


def _is_list_item(line: str) -> bool:
    """`- foo` / `* foo` / `1. foo`. Indented continuations of a
    bullet aren't bullets themselves; we treat them as prose so they
    join into the bullet's text on output."""
    stripped = line.lstrip()
    if line != stripped:  # indented — continuation
        return False
    if stripped.startswith(("- ", "* ")):
        return True
    return bool(re.match(r"\d+\.\s", stripped))


def _rewrite_heading(line: str) -> str:
    """`### Added` → `### ✨ Added`. Idempotent — already-emojified
    headings pass through untouched."""
    m = re.match(r"^(#{1,6})\s+(.*)$", line.rstrip())
    if not m:
        return line
    hashes, title = m.group(1), m.group(2).strip()
    # Skip rewriting when the title already starts with a non-ASCII
    # glyph (covers our emoji + any future symbol prefix).
    if title and ord(title[0]) > 127:
        return line
    emoji = _SECTION_EMOJI.get(title)
    if not emoji:
        return line
    return f"{hashes} {emoji} {title}"


def transform(text: str) -> str:
    """Apply the full transform to a CHANGELOG section body."""
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []
    in_fence = False
    in_list_item = False  # last non-blank line was a bullet or its continuation
    current_list_buf: list[str] = []
    summary_emitted = False
    saw_section_heading = False

    def _join(lines: list[str]) -> str:
        """Join hard-wrapped lines back into a single line. Soft-hyphen
        edge case: when a line ends in ``\\w-`` (letter + hyphen) the
        hyphen was a wrap point inside a hyphenated word (``per-`` ↦
        ``framework`` ↦ ``per-framework``). Don't insert a space in
        that case. A line ending in `` -`` (space-hyphen) — the em-
        dash convention — keeps the space."""
        result: list[str] = []
        for raw in lines:
            piece = raw.strip()
            if not piece:
                continue
            if result and re.search(r"\w-$", result[-1]):
                result[-1] = result[-1] + piece
            else:
                result.append(piece)
        return " ".join(result)

    def flush_para() -> None:
        nonlocal summary_emitted, saw_section_heading
        if not para:
            return
        joined = _join(para)
        if joined:
            # The first prose paragraph becomes the "Highlights"
            # section. Everything else is just prose between
            # bullet blocks.
            if not summary_emitted and not saw_section_heading:
                out.append("### 🚀 Highlights")
                out.append("")
                summary_emitted = True
            out.append(joined)
        para.clear()

    def flush_list_item() -> None:
        nonlocal in_list_item
        if current_list_buf:
            out.append(_join(current_list_buf))
            current_list_buf.clear()
        in_list_item = False

    for raw in lines:
        line = raw.rstrip()
        # Fenced code block — pass through verbatim. ``in_fence``
        # toggles on every ``` line.
        if line.startswith("```"):
            flush_para()
            flush_list_item()
            out.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(raw)
            continue

        if line.strip() == "":
            flush_para()
            flush_list_item()
            out.append("")
            continue

        if line.lstrip().startswith("#"):
            flush_para()
            flush_list_item()
            out.append(_rewrite_heading(line))
            saw_section_heading = True
            continue

        if _is_list_item(line):
            flush_para()
            flush_list_item()
            current_list_buf.append(line)
            in_list_item = True
            continue

        # Indented continuation of a bullet (the typical
        # "  continuation text" two-space indent or wrapped at any
        # leading whitespace).
        if in_list_item and (line.startswith("  ") or line.startswith("\t")):
            current_list_buf.append(line)
            continue

        # Otherwise — plain prose. If we were inside a bullet, the
        # prose line ends the bullet block.
        flush_list_item()
        para.append(line)

    flush_para()
    flush_list_item()

    # Trim trailing blank lines.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def main() -> None:
    sys.stdout.write(transform(sys.stdin.read()))


if __name__ == "__main__":
    main()

"""Slot-image URL re-fire-nonce version gate (#419).

The control plane appends a per-apply nonce as a URL ``#fragment`` so a fresh
apply of the same image re-fires the supervisor trigger. The host runner only
strips that fragment before fetching as of #386 (2026-06-12); an older
appliance hands it straight to the downloader and the apply wedges at
"in-flight" forever. ``_supervisor_strips_url_fragment`` gates the nonce so
only known-capable supervisors get it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.appliance import supervisor as sup


@pytest.mark.parametrize(
    ("supervisor_version", "installed_version", "expected"),
    [
        # ≥ 2026.06.12 strips the fragment → nonce is safe.
        ("2026.06.13-2", None, True),
        ("2026.06.12-1", None, True),
        ("2026.06.12", None, True),  # exactly the threshold
        ("2026.07.01-1", None, True),
        ("2027.01.01-1", None, True),
        # Pre-2026.06.12 does NOT strip → must get a clean URL.
        ("2026.06.11-1", None, False),  # Nuvopact's box
        ("2026.05.30-1", None, False),
        ("2025.12.31-9", None, False),
        # Falls back to installed_appliance_version when supervisor_version
        # is absent (older heartbeats).
        (None, "2026.06.14-1", True),
        (None, "2026.06.11-1", False),
        # Unknown / dev / empty → safe clean-URL path.
        (None, None, False),
        ("dev-abc1234", None, False),
        ("", "", False),
    ],
)
def test_supervisor_strips_url_fragment(
    supervisor_version: str | None,
    installed_version: str | None,
    expected: bool,
) -> None:
    row = SimpleNamespace(
        supervisor_version=supervisor_version,
        installed_appliance_version=installed_version,
    )
    assert sup._supervisor_strips_url_fragment(row) is expected  # type: ignore[arg-type]

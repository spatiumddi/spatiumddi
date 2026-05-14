"""State directory layout helpers.

The supervisor persists identity material (Ed25519 key + signed cert)
on the appliance's ``/var`` partition so a slot swap preserves the
identity verbatim. Wave A1 only ensures the layout exists; Wave A2
will write actual key/cert material here.
"""

from __future__ import annotations

from pathlib import Path


def ensure_layout(state_dir: Path) -> None:
    """Create the state-dir tree if missing. Idempotent."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "identity").mkdir(exist_ok=True)
    (state_dir / "tls").mkdir(exist_ok=True)

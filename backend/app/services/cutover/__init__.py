"""Windows → SpatiumDDI cutover workflow (issue #756).

The importers (#128 / #129) load a Windows DNS / DHCP estate into native rows
and stop there. This package is the rest of the journey:

* :mod:`.parity` — Phase 1. Diff what SpatiumDDI holds against what the Windows
  server is *currently* serving, and classify every difference.
* :mod:`.readiness` — the blocker set. Most importantly, it refuses to migrate
  an AD-integrated zone with "Secure only" dynamic updates, because GSS-TSIG is
  unimplemented (#444) and a DC that can't register its SRV records breaks AD.
* :mod:`.shadow` — Phase 2. Replay recently-observed production queries against
  both the Windows source and the managed targets and compare the answers.
* :mod:`.preflight` — Phase 3a. Lower TTLs ahead of the switch so a rollback
  propagates in minutes, snapshotting the originals so it can be undone exactly.
* :mod:`.leases` — Phase 3b. Hand the live Windows lease set over as
  reservations, so clients don't meet a Kea with an empty lease database.
* :mod:`.switch` — Phase 3c. The switch itself, and the rollback.
* :mod:`.checklist` — Phase 4. The decommission list.
* :mod:`.runbook` — the generated operator runbook.
* :mod:`.plans` — candidate discovery from the live Windows source.

Everything is per-zone / per-scope. There is no big-bang path on purpose.
"""

from __future__ import annotations

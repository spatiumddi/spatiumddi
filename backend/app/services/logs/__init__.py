"""Log line parsers for daemon-emitted log files.

The agents tail their daemon's log file and POST raw lines back to
the control plane. The control plane parses each line into
structured columns (timestamp, client IP, qname, qtype, etc) using
the modules in this package, then stores both the structured fields
*and* the raw original. Raw is preserved so:

* lines the parser doesn't fully understand still surface in the UI;
* the UI's "raw line" toggle lets operators see exactly what the
  daemon emitted, no parser layer in between.
"""

from __future__ import annotations

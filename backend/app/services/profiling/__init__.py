"""Device profiling — combine passive (DHCP fingerprint) + active (nmap)
device identification.

Phase 1 (this module) ships the active layer: a fresh DHCP lease in a
subnet with ``auto_profile_on_dhcp_lease=True`` enqueues an NmapScan
via the existing pipeline and stamps the result back onto the
``IPAddress`` row (``last_profiled_at`` + ``last_profile_scan_id``).

Phase 2 will add the passive layer (DHCP option-55/option-60 capture
+ fingerbank lookup) and write into the same surface. See CLAUDE.md
"Device profiling" entry for the full design.
"""

"""Appliance management service layer — Phase 4 (issue #134).

Modules:
    tls — certificate parsing, key/cert matching, fingerprint helpers
          for the Web UI cert management surface (Phase 4b).

Future Phase 4 modules will land alongside:
    releases   — GitHub release polling + pull-and-recycle (4c)
    containers — docker socket adapter (4d)
    logs       — journalctl + diagnostic bundle (4e)
    network    — host network + nftables + maintenance (4f)
"""

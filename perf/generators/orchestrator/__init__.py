"""SpatiumDDI perf — the diurnal device-fleet orchestrator (the long-pole load).

Components (each runnable as a script per the workers.py REGISTRY argv contract, and
importable as a module):

  * ``device_fleet.py``        — asyncio DHCP FSM fleet + DNS streams + propagation probe
  * ``dhcp_packet.py``         — raw DHCPv4 byte-template packet library (struct)
  * ``lifecycle_log.py``       — lifecycle NDJSON writer + HdrHistogram latency accumulator
  * ``api_mutation_stream.py`` — operator-API mutation stream (the audit-lock exerciser)
  * ``synthetic_ui_probe.py``  — low-rate human page-load probe (own latency/5xx SLO)
"""

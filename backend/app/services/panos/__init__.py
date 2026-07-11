"""Palo Alto PAN-OS / Panorama integration service package (issue #605).

``client`` — the async PAN-OS API client (REST for objects/NAT, legacy XML
for keygen / op-commands / User-ID DAG-tag register).
``reconcile`` — the read-only mirror reconciler (address objects/groups → the
``firewall_endpoint_object`` store, NAT rules → ``nat_mapping`` provenance
rows, optional zones/interfaces + DHCP leases → IPAM).

DAG enforcement (Shape 2 / the #601 tier) lives in
``app.services.block_sync.reconcile`` — it consumes this package's client to
register ``IP → tag`` via the User-ID API.
"""

"""Cloud integration (issue #37, Part A) — read-only infrastructure mirror.

``base`` defines the provider-neutral contracts (CloudInventory + the
CloudConnector ABC + the lazy connector registry). ``reconcile`` upserts
a CloudInventory into IPAM. Concrete connectors live in ``aws`` /
``azure`` / ``gcp`` and are resolved via ``base.get_connector`` — the
service layer never imports them directly.
"""

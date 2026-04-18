"""IPAM service layer — resize + future IPAM business logic.

Keep router handlers thin — all validation / mutation / cross-resource
cleanup lives here so the same logic is reachable from Celery tasks and
the REST API without drift.
"""

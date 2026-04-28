"""Exception hierarchy raised by the SNMP poller.

Every public function in ``app.services.snmp.poller`` either succeeds
or raises one of these. The Celery task layer maps each to a
``last_poll_status`` value so operators see a structured reason
without re-parsing pysnmp tracebacks.
"""

from __future__ import annotations


class SNMPError(Exception):
    """Base — never raised directly."""


class SNMPTimeoutError(SNMPError):
    """No response within ``snmp_timeout_seconds * (snmp_retries + 1)``.

    Most often a hostname / firewall / community-mismatch issue. v1
    has no notion of "auth failed" so the wire is silent on bad creds
    and we surface the same timeout for both cases.
    """


class SNMPAuthError(SNMPError):
    """SNMPv3 authentication or privacy failure (USM ``authError`` or
    ``decryptionError`` reports), or v2c reporting an
    ``authorizationError`` PDU. A v3-only condition in practice
    because v1 / v2c don't authenticate beyond the community string.
    """


class SNMPTransportError(SNMPError):
    """Socket-level problem — DNS resolution, host unreachable,
    refused connection. The poller wraps the underlying OSError
    message so operators can act on it.
    """


class SNMPProtocolError(SNMPError):
    """The agent answered but with a malformed / unexpected PDU
    (e.g. a varbind whose value doesn't match the type the OID
    promises). Usually indicates a buggy SNMP agent — the operator
    needs to tell us, not retry.
    """


__all__ = [
    "SNMPError",
    "SNMPTimeoutError",
    "SNMPAuthError",
    "SNMPTransportError",
    "SNMPProtocolError",
]

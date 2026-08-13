"""Inputs that used to crash instead of failing validation (fuzz round 4).

Each of these reached a place that counts BYTES or speaks ASCII-only
(cryptography's x509 Name, httpx's URL/header build) with a value pydantic
had already accepted, and surfaced as a 500. They are 422s.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_csr_country_must_be_two_ascii_letters() -> None:
    """pydantic's min/max_length counts characters; CountryName counts
    encoded bytes — a 2-char multibyte string blew up the x509 build."""
    from app.api.v1.appliance.tls import CSRGenerate

    ok = CSRGenerate(name="n", common_name="cn", country="ca")
    assert ok.country == "ca"
    with pytest.raises(ValidationError):
        CSRGenerate(name="n", common_name="cn", country="毆毆")
    with pytest.raises(ValidationError):
        CSRGenerate(name="n", common_name="cn", country="USA")


@pytest.mark.parametrize("bad", ["http://x/\U000482f8", "key毆", "a\nb"])
def test_import_credentials_must_be_printable_ascii(bad: str) -> None:
    """A URL / API key travels as a URL and an HTTP header — ASCII on the
    wire. Non-ASCII (or control chars) crashed the outbound client."""
    import uuid

    from app.api.v1.dns_import.router import PowerDNSPreviewIn, TechnitiumTestIn
    from app.api.v1.netbox_import.router import NetboxConnIn

    with pytest.raises(ValidationError):
        PowerDNSPreviewIn(api_url=bad, api_key="k", target_group_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        TechnitiumTestIn(api_url="http://t", api_token=bad)
    with pytest.raises(ValidationError):
        NetboxConnIn(base_url="http://n", token=bad)

"""The agent bootstrap PSK gates answer 401 to ANY wrong key — never 500.

``hmac.compare_digest`` raises TypeError on a str containing non-ASCII
characters, so a client that sent one (observed live from a conformance
fuzzer: ``X-DNS-Agent-Key: "\x80"``) got a 500 out of the auth gate
instead of the 401 a wrong key is. The gates now compare bytes.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "header"),
    [
        ("/api/v1/dns/agents/register", "X-DNS-Agent-Key"),
        ("/api/v1/dhcp/agents/register", "X-DHCP-Agent-Key"),
        ("/api/v1/looking-glass/agents/register", "X-LG-Agent-Key"),
    ],
)
async def test_non_ascii_psk_is_401_not_500(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str, header: str
) -> None:
    for var in ("DNS_AGENT_KEY", "DHCP_AGENT_KEY", "LG_AGENT_KEY"):
        monkeypatch.setenv(var, "expected-key")
    resp = await client.post(
        path,
        # httpx refuses non-ASCII str header values; bytes take the wire
        # path requests used (latin-1), which is what the fuzzer sent.
        headers={header.encode("latin-1"): "\x80".encode("latin-1")},
        json={"hostname": "h1", "fingerprint": "f1"},
    )
    assert resp.status_code == 401, resp.text

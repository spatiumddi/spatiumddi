"""Unit tests for app.core.auth.oidc — joserfc migration (#187).

These tests exercise the full exchange_code() path using real RSA-signed JWTs
and httpx.MockTransport so that no internal functions are patched.
All HTTP I/O (discovery, JWKS, token endpoint, userinfo) runs through the mock.
"""
from __future__ import annotations

import inspect
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet, RSAKey

# ---------------------------------------------------------------------------
# Helpers — real RSA key pair + JWT minting
# ---------------------------------------------------------------------------
_priv_rsa = rsa.generate_private_key(65537, 2048)
_priv_pem = _priv_rsa.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_pub_pem = _priv_rsa.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
_priv_key = RSAKey.import_key(_priv_pem, {"use": "sig", "kid": "test-key"})
_pub_key = RSAKey.import_key(_pub_pem, {"use": "sig", "kid": "test-key"})
_jwks_dict = KeySet([_pub_key]).as_dict()

ISSUER = "https://idp.example.com"
CLIENT_ID = "spatiumddi-test"
NONCE = "test-nonce-abc123"


def _mint(
    iss: str = ISSUER,
    aud: str = CLIENT_ID,
    sub: str = "user-1",
    exp_offset: int = 3600,
    nonce: str = NONCE,
    **extra,
) -> str:
    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
        "nonce": nonce,
        "preferred_username": "tuser",
        "email": "tuser@example.com",
        "name": "Test User",
        "groups": ["admins"],
        **extra,
    }
    return joserfc_jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, _priv_key)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
DISCOVERY_DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/auth",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
    "userinfo_endpoint": f"{ISSUER}/userinfo",
}


def _make_transport(id_token: str, token_status: int = 200, userinfo: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openid-configuration" in url:
            return httpx.Response(200, json=DISCOVERY_DOC)
        if "jwks.json" in url:
            return httpx.Response(200, json=_jwks_dict)
        if "/token" in url:
            if token_status != 200:
                return httpx.Response(token_status, text="error")
            return httpx.Response(
                200,
                json={"id_token": id_token, "access_token": "acc-tok", "token_type": "Bearer"},
            )
        if "/userinfo" in url:
            if userinfo:
                return httpx.Response(200, json=userinfo)
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _patched_client(transport):
    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(transport=transport, **kwargs)

    return _Client


# ---------------------------------------------------------------------------
# Import-guard tests
# ---------------------------------------------------------------------------
class TestNoAuthlibImports:
    def test_no_authlib_in_module_source(self):
        import app.core.auth.oidc as mod

        src = inspect.getsource(mod)
        assert "authlib" not in src, "authlib import found in oidc.py — migration incomplete"

    def test_joserfc_used(self):
        import app.core.auth.oidc as mod

        src = inspect.getsource(mod)
        assert "joserfc" in src


# ---------------------------------------------------------------------------
# Happy-path integration tests (full HTTP path, real RSA JWT)
# ---------------------------------------------------------------------------
class TestExchangeCodeHappyPath:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from app.core.auth.oidc import invalidate_caches
        invalidate_caches("test-provider")
        yield
        invalidate_caches("test-provider")

    @pytest.fixture
    def cfg(self):
        from app.core.auth.oidc import OIDCConfig
        return OIDCConfig(
            discovery_url=f"{ISSUER}/.well-known/openid-configuration",
            client_id=CLIENT_ID,
            client_secret="secret",
            scopes=["openid", "profile", "email"],
            claim_username="preferred_username",
            claim_email="email",
            claim_display_name="name",
            claim_groups="groups",
        )

    @pytest.mark.asyncio
    async def test_valid_token_returns_result(self, cfg):
        from app.core.auth.oidc import exchange_code

        token = _mint()
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            result = await exchange_code(cfg, "test-provider", "code", "https://app/cb", NONCE)

        assert result.external_id == "user-1"
        assert result.username == "tuser"
        assert result.email == "tuser@example.com"
        assert result.groups == ["admins"]

    @pytest.mark.asyncio
    async def test_aud_as_list_accepted(self, cfg):
        from app.core.auth.oidc import exchange_code

        token = _mint(aud=[CLIENT_ID, "other-client"])
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            result = await exchange_code(cfg, "test-provider", "code", "https://app/cb", NONCE)

        assert result.external_id == "user-1"

    @pytest.mark.asyncio
    async def test_groups_as_string_normalised(self, cfg):
        from app.core.auth.oidc import exchange_code

        token = _mint(groups="admins")
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            result = await exchange_code(cfg, "test-provider", "code", "https://app/cb", NONCE)

        assert result.groups == ["admins"]


# ---------------------------------------------------------------------------
# Token-validation rejection tests
# ---------------------------------------------------------------------------
class TestExchangeCodeTokenValidation:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from app.core.auth.oidc import invalidate_caches
        invalidate_caches("val-provider")
        yield
        invalidate_caches("val-provider")

    @pytest.fixture
    def cfg(self):
        from app.core.auth.oidc import OIDCConfig
        return OIDCConfig(
            discovery_url=f"{ISSUER}/.well-known/openid-configuration",
            client_id=CLIENT_ID,
            client_secret="secret",
            scopes=["openid"],
            claim_username="preferred_username",
            claim_email="email",
            claim_display_name="name",
            claim_groups="groups",
        )

    @pytest.mark.asyncio
    async def test_wrong_issuer_rejected(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        token = _mint(iss="https://attacker.example.com")
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            with pytest.raises(OIDCServiceError, match="ID token invalid"):
                await exchange_code(cfg, "val-provider", "code", "https://app/cb", NONCE)

    @pytest.mark.asyncio
    async def test_wrong_audience_rejected(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        token = _mint(aud="some-other-client")
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            with pytest.raises(OIDCServiceError, match="ID token invalid"):
                await exchange_code(cfg, "val-provider", "code", "https://app/cb", NONCE)

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        token = _mint(exp_offset=-7200)
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            with pytest.raises(OIDCServiceError, match="ID token invalid"):
                await exchange_code(cfg, "val-provider", "code", "https://app/cb", NONCE)

    @pytest.mark.asyncio
    async def test_nonce_mismatch_rejected(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        token = _mint(nonce="wrong-nonce")
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(token))):
            with pytest.raises(OIDCServiceError, match="nonce mismatch"):
                await exchange_code(cfg, "val-provider", "code", "https://app/cb", NONCE)

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        token = _mint()
        # Flip last char to corrupt signature
        tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(_make_transport(tampered))):
            with pytest.raises(OIDCServiceError, match="ID token invalid"):
                await exchange_code(cfg, "val-provider", "tampered", "https://app/cb", NONCE)

    @pytest.mark.asyncio
    async def test_missing_id_token_raises(self, cfg):
        from app.core.auth.oidc import OIDCServiceError, exchange_code

        def handler(request):
            url = str(request.url)
            if "openid-configuration" in url:
                return httpx.Response(200, json=DISCOVERY_DOC)
            if "jwks.json" in url:
                return httpx.Response(200, json=_jwks_dict)
            if "/token" in url:
                return httpx.Response(200, json={"access_token": "acc", "token_type": "Bearer"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        with patch("app.core.auth.oidc.httpx.AsyncClient", _patched_client(transport)):
            with pytest.raises(OIDCServiceError, match="no id_token"):
                await exchange_code(cfg, "val-provider", "code", "https://app/cb", NONCE)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
class TestOIDCConfigValidation:
    def test_missing_discovery_url(self):
        from app.core.auth.oidc import OIDCConfig
        import pydantic
        with pytest.raises((ValueError, pydantic.ValidationError)):
            OIDCConfig(client_id="x", client_secret="y")

    def test_missing_client_id(self):
        from app.core.auth.oidc import OIDCConfig
        import pydantic
        with pytest.raises((ValueError, pydantic.ValidationError)):
            OIDCConfig(discovery_url="https://idp/openid-configuration", client_secret="y")


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------
class TestInvalidateCaches:
    def test_invalidate_removes_cached_entries(self):
        from app.core.auth.oidc import _discovery_cache, _jwks_cache, invalidate_caches

        _discovery_cache["p1"] = ("doc", time.time() + 300)
        _jwks_cache["p1"] = ({"keys": []}, time.time() + 300)
        invalidate_caches("p1")
        assert "p1" not in _discovery_cache
        assert "p1" not in _jwks_cache

    def test_invalidate_unknown_provider_is_noop(self):
        from app.core.auth.oidc import invalidate_caches
        invalidate_caches("nonexistent-provider-xyz")  # must not raise

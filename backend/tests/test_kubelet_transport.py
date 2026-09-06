"""The kubelet Summary API transport picker (#983 Phase 2 item 6).

Two ways to reach the same document with very different blast radius:

  proxy   apiserver ``nodes/proxy [get]`` — authorizes read GETs to EVERY
          kubelet endpoint (/pods, /logs/…, /configz, /debug/…).
  direct  the kubelet's own ``nodes/stats [get]`` — that one page only.

Direct is tried first with the proxy as fallback, because whether the
ServiceAccount CA validates a given cluster's kubelet serving cert is a
property of the deployment, not something the chart can assert — k3s signs
kubelet serving certs with its own ``server-ca``. Nothing in this repo had
ever spoken to a kubelet directly before this (#983 named the TTY console as
a reference; it also uses the apiserver proxy), so the code finds out at
runtime and reports which transport served.

What is asserted here is the DECISION LOGIC, not TLS: that a direct failure
falls back rather than surfacing as missing metrics, that a failure which
will not change on the next tick is not retried on every poll, and that the
reported transport tells the truth. Whether the CA actually verifies is a
hardware question these tests cannot answer and deliberately do not fake.
"""

from __future__ import annotations

import http.client
import json
import ssl

import pytest

from app.services.appliance import k8s


@pytest.fixture(autouse=True)
def _reset_transport_state():
    """The per-node block cache is a module global — a leak between tests
    would make one test's fallback look like another's."""
    k8s._kubelet_direct_blocked.clear()
    yield
    k8s._kubelet_direct_blocked.clear()


@pytest.fixture
def _ca_file(tmp_path):
    """A real, loadable CA bundle.

    ``ssl.create_default_context(cafile=...)`` genuinely parses the file, so a
    placeholder path makes every test here fail inside TLS setup instead of in
    the logic under test. Generating one keeps these hermetic — no system
    trust store, no network.
    """
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-kubelet-ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


@pytest.fixture
def _cfg(monkeypatch, _ca_file):
    cfg = k8s._Config(
        host="10.43.0.1", port=443, token="tok", ca_path=_ca_file, namespace="spatium"
    )
    monkeypatch.setattr(k8s, "get_config", lambda: cfg)
    return cfg


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeSocket:
    """Only what the code under test touches: ``settimeout``.

    Records every value, because #993's fix is precisely that the socket is
    re-armed after connect — a fake that accepted the call and forgot it
    would let the read budget regress to the connect budget silently.
    """

    def __init__(self):
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _FakeConn:
    """Stands in for HTTPSConnection; records what it was asked for."""

    last: _FakeConn | None = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        # The timeout handed to the CONSTRUCTOR is the connect budget; the
        # one handed to sock.settimeout() after connect is the read budget.
        self.init_timeout = timeout
        self.requested: tuple[str, str] | None = None
        self.headers: dict[str, str] = {}
        self.closed = False
        self.connected = False
        self.sock: _FakeSocket | None = None
        self.raises: BaseException | None = None
        self.connect_raises: BaseException | None = None
        self.response = _FakeResponse(200, b"{}")
        _FakeConn.last = self

    def connect(self):
        """Mirrors http.client: opens the socket, or raises trying.

        ``sock`` stays None on failure, which is why the code under test
        guards on it — a raising connect must not then be followed by an
        attribute error on the way to the fallback.
        """
        if self.connect_raises is not None:
            raise self.connect_raises
        self.connected = True
        self.sock = _FakeSocket()

    def request(self, method, path, headers=None):
        self.requested = (method, path)
        self.headers = headers or {}
        if self.raises is not None:
            raise self.raises

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _install(monkeypatch, *, status=200, body=b"{}", raises=None, connect_raises=None):
    def _factory(host, port, timeout=None, context=None):
        conn = _FakeConn(host, port, timeout, context)
        conn.response = _FakeResponse(status, body)
        conn.raises = raises
        conn.connect_raises = connect_raises
        return conn

    monkeypatch.setattr(http.client, "HTTPSConnection", _factory)


# ── the happy path ──────────────────────────────────────────────────────────


def test_direct_is_used_when_it_works(monkeypatch, _cfg):
    payload = {"node": {"nodeName": "ddi1"}}
    _install(monkeypatch, body=json.dumps(payload).encode())
    status, parsed, transport = k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert (status, parsed, transport) == (200, payload, "direct")
    assert _FakeConn.last.port == 10250
    assert _FakeConn.last.requested == ("GET", "/stats/summary")
    assert _FakeConn.last.headers["Authorization"] == "Bearer tok"
    assert k8s.kubelet_block_reasons() == {}


def test_connection_is_always_closed(monkeypatch, _cfg):
    _install(monkeypatch, raises=OSError("boom"))
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert _FakeConn.last.closed is True


# ── falling back ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raises": ssl.SSLCertVerificationError("bad CA")},
        {"raises": OSError("connection refused")},
        {"status": 403},
        {"status": 401},
        {"status": 500},
    ],
)
def test_any_direct_failure_falls_back_to_the_proxy(monkeypatch, _cfg, kwargs):
    """A transport problem must never surface as missing metrics — that is
    what would make the Cluster screen go blank on upgrade day."""
    _install(monkeypatch, **kwargs)
    payload = {"node": {"nodeName": "ddi1"}}
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, json.dumps(payload).encode()))
    status, parsed, transport = k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert (status, parsed, transport) == (200, payload, "proxy")


def test_no_node_ip_goes_straight_to_the_proxy(monkeypatch, _cfg):
    """Callers that do not know the address must not be broken by item 6."""
    calls: list[str] = []

    def _fake_request(method, path, **kw):
        calls.append(path)
        return 200, b'{"node": {}}'

    _install(monkeypatch, raises=AssertionError("direct must not be attempted"))
    monkeypatch.setattr(k8s, "_request", _fake_request)
    status, _, transport = k8s.get_node_stats_summary("ddi1")
    assert (status, transport) == (200, "proxy")
    assert calls == ["/api/v1/nodes/ddi1/proxy/stats/summary"]


def test_ca_verification_failure_names_the_remedy(monkeypatch, _cfg):
    """This is THE question the arrangement exists to answer, so the reason
    has to carry the fix rather than just the exception."""
    _install(monkeypatch, raises=ssl.SSLCertVerificationError("unable to get local issuer"))
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    reason = k8s.kubelet_block_reasons()["192.168.0.199"]
    assert "SPATIUM_KUBELET_CA_PATH" in reason
    assert "server-ca.crt" in reason


def test_rbac_refusal_points_at_the_grant(monkeypatch, _cfg):
    _install(monkeypatch, status=403)
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert "nodes/stats" in k8s.kubelet_block_reasons()["192.168.0.199"]


# ── not retrying a settled failure ──────────────────────────────────────────


def test_a_blocked_direct_path_is_not_retried_every_poll(monkeypatch, _cfg):
    """The health poll fans out over every node once a minute. A handshake
    against a CA that cannot verify costs a round trip each time, so a
    verdict that will not change has to stick."""
    attempts = {"n": 0}

    def _factory(host, port, timeout=None, context=None):
        attempts["n"] += 1
        conn = _FakeConn(host, port, timeout, context)
        conn.raises = ssl.SSLCertVerificationError("bad CA")
        return conn

    monkeypatch.setattr(http.client, "HTTPSConnection", _factory)
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    for _ in range(5):
        k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert attempts["n"] == 1
    assert "192.168.0.199" in k8s.kubelet_block_reasons()


def test_the_block_expires_so_a_transient_failure_self_heals(monkeypatch, _cfg):
    """A kubelet restarting is not a permanent verdict; the direct path has
    to come back on its own or the fallback becomes forever."""
    _install(monkeypatch, raises=OSError("connection refused"))
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    assert k8s.get_node_stats_summary("ddi1", "192.168.0.199")[2] == "proxy"

    # Wind the clock past the retry interval and make direct work.
    k8s._kubelet_direct_blocked["192.168.0.199"] = (0.0, "stale")
    _install(monkeypatch, body=b'{"node": {}}')
    assert k8s.get_node_stats_summary("ddi1", "192.168.0.199")[2] == "direct"
    # ...and the reason went WITH the block. A recovered node reporting
    # "direct" alongside a stale CA error is a status line that contradicts
    # itself; the reason lives inside the block entry so it cannot outlive it.
    assert k8s.kubelet_block_reasons() == {}


def test_an_unparseable_200_does_not_block_the_direct_path(monkeypatch, _cfg):
    """A body we cannot parse is a data problem, not a transport verdict —
    blocking on it would demote the cluster to the broad grant over one bad
    response."""
    _install(monkeypatch, body=b"not json")
    status, parsed, transport = k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert (status, parsed, transport) == (200, None, "direct")
    assert k8s.kubelet_block_reasons() == {}


# ── CA selection ────────────────────────────────────────────────────────────


def test_ca_defaults_to_the_service_account_bundle(_cfg, _ca_file):
    assert k8s._kubelet_ca_path(_cfg) == _ca_file


def test_ca_override_wins(monkeypatch, _cfg):
    """The escape hatch for a cluster whose kubelet serving certs are signed
    by a different CA than the apiserver's — k3s's server-ca."""
    monkeypatch.setenv("SPATIUM_KUBELET_CA_PATH", "/etc/ssl/kubelet-ca.crt")
    assert k8s._kubelet_ca_path(_cfg) == "/etc/ssl/kubelet-ca.crt"


def test_an_unloadable_ca_bundle_degrades_instead_of_500ing(monkeypatch, _cfg):
    """``SPATIUM_KUBELET_CA_PATH`` is operator-supplied, and
    ``create_default_context`` raises on a path that is missing or is not a
    PEM. That exception escaping would sail past cluster_health's
    KubeapiUnavailableError-only handler and take down the whole Cluster
    screen — over a setting whose entire purpose is to fix the direct path.
    Found by this test on the first run."""
    monkeypatch.setenv("SPATIUM_KUBELET_CA_PATH", "/nonexistent/ca.crt")
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b'{"node": {}}'))
    status, parsed, transport = k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert (status, parsed, transport) == (200, {"node": {}}, "proxy")
    assert "cannot load kubelet CA bundle" in k8s.kubelet_block_reasons()["192.168.0.199"]


# ── per-node isolation (the review's finding, and the reason it matters) ────


def test_one_bad_node_does_not_demote_the_others(monkeypatch, _cfg):
    """A single kubelet restarting must not push the WHOLE cluster onto the
    apiserver proxy for 15 minutes. With ``kubeletProxyFallback: false`` that
    is not a demotion — it is total loss of live metrics for every node
    because one of them blinked."""
    bad_ip = "192.168.0.199"

    def _factory(host, port, timeout=None, context=None):
        conn = _FakeConn(host, port, timeout, context)
        if host == bad_ip:
            conn.raises = OSError("connection refused")
        else:
            conn.response = _FakeResponse(200, b'{"node": {}}')
        return conn

    monkeypatch.setattr(http.client, "HTTPSConnection", _factory)
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b'{"node": {}}'))

    assert k8s.get_node_stats_summary("ddi1", bad_ip)[2] == "proxy"
    assert k8s.get_node_stats_summary("ddi2", "192.168.0.200")[2] == "direct"
    assert k8s.get_node_stats_summary("ddi3", "192.168.0.201")[2] == "direct"
    # ...and only the bad one carries a reason.
    assert list(k8s.kubelet_block_reasons()) == [bad_ip]


def test_the_block_is_keyed_by_node_not_shared(monkeypatch, _cfg):
    """Second half of the same property: the blocked node stays blocked on
    the next poll while its neighbours keep going direct."""
    bad_ip = "192.168.0.199"
    attempts: dict[str, int] = {}

    def _factory(host, port, timeout=None, context=None):
        attempts[host] = attempts.get(host, 0) + 1
        conn = _FakeConn(host, port, timeout, context)
        if host == bad_ip:
            conn.raises = ssl.SSLCertVerificationError("bad CA")
        else:
            conn.response = _FakeResponse(200, b'{"node": {}}')
        return conn

    monkeypatch.setattr(http.client, "HTTPSConnection", _factory)
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b'{"node": {}}'))
    for _ in range(3):
        k8s.get_node_stats_summary("ddi1", bad_ip)
        k8s.get_node_stats_summary("ddi2", "192.168.0.200")
    assert attempts[bad_ip] == 1  # probed once, then backed off
    assert attempts["192.168.0.200"] == 3  # unaffected


# ── #993 — the connect budget and the read budget are separate ──────────────


def test_connect_is_short_and_the_read_budget_is_longer(monkeypatch, _cfg):
    """One socket timeout would have to serve both, and cannot.

    1.5 s is right for a LAN handshake and far too tight for the response:
    a busy node marshalling /stats/summary for a few hundred pods can
    legitimately take seconds, and under a single 1.5 s budget it would be
    blocked-direct for 15 minutes with a reason indistinguishable from the
    firewall failure #993 exists to fix. So connect gets the short budget
    and the open socket is re-armed with the long one.
    """
    _install(monkeypatch)
    k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    conn = _FakeConn.last
    assert conn.init_timeout == k8s._KUBELET_CONNECT_TIMEOUT_S
    assert conn.sock is not None
    assert conn.sock.timeouts == [k8s._KUBELET_READ_TIMEOUT_S]
    # The point of the split, stated as a relation rather than as two
    # literals: a future retune that collapses them is the regression.
    assert k8s._KUBELET_READ_TIMEOUT_S > k8s._KUBELET_CONNECT_TIMEOUT_S


def test_the_socket_is_rearmed_before_the_request_is_sent(monkeypatch, _cfg):
    """Re-arming after the response would be useless. Ordering is the whole
    behaviour, and a fake that only counted calls could not see it."""
    _install(monkeypatch)
    order: list[str] = []

    real_factory = http.client.HTTPSConnection

    def _factory(host, port, timeout=None, context=None):
        conn = real_factory(host, port, timeout=timeout, context=context)
        orig_connect, orig_request = conn.connect, conn.request

        def connect():
            orig_connect()
            conn.sock.settimeout = lambda v: order.append(f"settimeout({v})")

        def request(*a, **kw):
            order.append("request")
            return orig_request(*a, **kw)

        conn.connect, conn.request = connect, request
        return conn

    monkeypatch.setattr(http.client, "HTTPSConnection", _factory)
    k8s.get_node_stats_summary("ddi1", "192.168.0.199")
    assert order == [f"settimeout({k8s._KUBELET_READ_TIMEOUT_S})", "request"]


def test_a_connect_failure_still_falls_back_to_the_proxy(monkeypatch, _cfg):
    """``sock`` stays None when connect raises, and the code reads it. A
    naive ``conn.sock.settimeout(...)`` would raise AttributeError there —
    inside the direct path, on the way to a fallback that never happens.
    """
    _install(monkeypatch, connect_raises=TimeoutError("timed out"))
    monkeypatch.setattr(k8s, "_request", lambda *a, **k: (200, b"{}"))
    assert k8s.get_node_stats_summary("ddi1", "192.168.0.199")[2] == "proxy"
    assert "TimeoutError" in k8s.kubelet_block_reasons()["192.168.0.199"]
    # …and the connection is still closed on that path.
    assert _FakeConn.last.closed is True

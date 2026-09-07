"""#980 — the two Kea packet-path settings, in both daemon blocks.

Both are rendered rather than left to Kea's defaults, and both defaults were
chosen from measurement against kea-dhcp4 3.0.3 (see
``DHCPServerGroup.kea_thread_pool_size`` for the numbers). What these tests
protect is the wiring: a value that is stored, shipped and then not rendered
is a silent no-op, which is the failure class #430 / #734 / #899 each found
one instance of.
"""

from __future__ import annotations

import pytest

from spatium_dhcp_agent.render_kea import render


def _bundle(**server_extra) -> dict:
    server = {"name": "dhcp1", "interfaces": ["eth0"]}
    server.update(server_extra)
    return {
        "server": server,
        "global_options": {"lease_time": 3600},
        "scopes": [
            {
                "subnet_cidr": "192.0.2.0/24",
                "lease_time": 3600,
                "pools": [
                    {
                        "start_ip": "192.0.2.100",
                        "end_ip": "192.0.2.200",
                        "pool_type": "dynamic",
                    }
                ],
                "options": {},
            },
            {
                "subnet_cidr": "2001:db8::/64",
                "address_family": "ipv6",
                "v6_address_mode": "stateful",
                "lease_time": 3600,
                "pools": [
                    {
                        "start_ip": "2001:db8::100",
                        "end_ip": "2001:db8::200",
                        "pool_type": "dynamic",
                    }
                ],
                "options": {},
            },
        ],
    }


# ── thread-pool-size ────────────────────────────────────────────────────────


@pytest.mark.parametrize("daemon", ["Dhcp4", "Dhcp6"])
def test_thread_pool_size_is_rendered_from_the_bundle(daemon):
    out = render(_bundle(kea_thread_pool_size=3))[daemon]
    assert out["multi-threading"]["thread-pool-size"] == 3


@pytest.mark.parametrize("daemon", ["Dhcp4", "Dhcp6"])
def test_multi_threading_stays_enabled_at_a_pool_of_one(daemon):
    """A pool of one is a resize, NOT ``enable-multi-threading: false``.

    With MT off, one thread must both receive and process; measured, that
    was 15,170 socket drops in a run where a pool of one had none. It also
    changes host-reservation lookup order and re-enables
    ``dhcp-queue-control``. Rendering it by accident would trade a
    throughput win for a semantics change nobody asked for.
    """
    out = render(_bundle(kea_thread_pool_size=1))[daemon]
    assert out["multi-threading"]["enable-multi-threading"] is True


@pytest.mark.parametrize("daemon", ["Dhcp4", "Dhcp6"])
def test_zero_is_passed_through_as_keas_auto_sizing(daemon):
    """0 is a real value — the operator opting back into Kea's own sizing —
    and must not be coalesced to the default it exists to escape."""
    out = render(_bundle(kea_thread_pool_size=0))[daemon]
    assert out["multi-threading"]["thread-pool-size"] == 0


@pytest.mark.parametrize("daemon", ["Dhcp4", "Dhcp6"])
def test_a_bundle_from_an_older_control_plane_gets_one_not_auto(daemon):
    """Absent means 1, not 0. "The field is missing" and "the operator asked
    for auto" must not render the same way: auto is the behaviour this
    setting exists to replace."""
    out = render(_bundle())[daemon]
    assert out["multi-threading"]["thread-pool-size"] == 1


# ── per-packet logging ──────────────────────────────────────────────────────


def _loggers(out, name):
    return [lg for lg in out["loggers"] if lg["name"] == name]


@pytest.mark.parametrize(
    ("daemon", "root"), [("Dhcp4", "kea-dhcp4"), ("Dhcp6", "kea-dhcp6")]
)
def test_packet_logging_on_by_default_leaves_the_child_alone(daemon, root):
    """Default must be byte-identical to what shipped before #980: no
    override at all, so the child inherits the parent's INFO."""
    out = render(_bundle())[daemon]
    assert _loggers(out, f"{root}.packets") == []
    assert _loggers(out, root)[0]["severity"] == "INFO"


@pytest.mark.parametrize(
    ("daemon", "root"), [("Dhcp4", "kea-dhcp4"), ("Dhcp6", "kea-dhcp6")]
)
def test_disabling_packet_logging_raises_only_that_child(daemon, root):
    out = render(_bundle(kea_packet_logging=False))[daemon]
    quiet = _loggers(out, f"{root}.packets")
    assert len(quiet) == 1
    assert quiet[0]["severity"] == "WARN"
    # The parent stays at INFO, or this would silence the lease lines the
    # Logs tab is built on.
    assert _loggers(out, root)[0]["severity"] == "INFO"


@pytest.mark.parametrize(
    ("daemon", "root"), [("Dhcp4", "kea-dhcp4"), ("Dhcp6", "kea-dhcp6")]
)
def test_the_daemon_child_logger_is_never_silenced(daemon, root):
    """``kea-dhcpN.dhcpN`` looks like the same kind of noise and is not.

    At INFO it carries ``DHCP4_OPEN_SOCKETS_FAILED`` — a real failure Kea
    logs at INFO — plus ``DHCP4_CONFIG_COMPLETE``, ``DHCP4_STARTED`` and
    ``DHCP4_MULTI_THREADING_INFO``, the last being the only line that
    reports whether the pool size above actually took effect. Verified
    against kea-dhcp4 3.0.3: silencing it loses all four.
    """
    out = render(_bundle(kea_packet_logging=False))[daemon]
    assert _loggers(out, f"{root}.{daemon.lower()}") == []


@pytest.mark.parametrize(
    ("daemon", "root"), [("Dhcp4", "kea-dhcp4"), ("Dhcp6", "kea-dhcp6")]
)
def test_the_quiet_child_keeps_the_parents_appenders(daemon, root):
    """log4cplus does not inherit appenders into a logger configured by
    name, so a child with no ``output_options`` sends its WARNs and ERRORs
    nowhere at all — which would be a silencing, not a level change."""
    out = render(_bundle(kea_packet_logging=False))[daemon]
    quiet = _loggers(out, f"{root}.packets")[0]
    outputs = {o["output"] for o in quiet["output_options"]}
    assert outputs == {"stdout", f"/var/log/kea/{root}.log"}

# spatium-supervisor

Host-side supervisor container for SpatiumDDI **Application**-role
appliances. See [#170] for the umbrella design.

## What it owns (eventually)

- **Identity** — Ed25519 keypair, control-plane-signed cert, persistent
  on `/var` so it survives slot swaps.
- **Service container orchestration** — pulls + starts + stops the
  `dns-bind9` / `dns-powerdns` / `dhcp-kea` images per the role the
  control plane assigns; never bakes them into the appliance image.
- **Firewall** — renders an `nftables` drop-in per active role
  (always-open SSH/ICMP/loopback + per-role service ports + operator
  override).
- **System telemetry** — slot state, docker ps, system vitals reported
  to the control plane on heartbeat (replaces today's per-agent
  `slot_state.py` collectors).

## What it owns *right now* (Phase A1 — this PR)

Nothing. The container builds, boots, logs an idle line every 60 s,
and exits cleanly on SIGTERM. Wave A2 adds the identity + register
flow; Wave B adds approval; Wave C adds role assignment; Wave D adds
the fleet UI.

## Layout

```
agent/supervisor/
  pyproject.toml              setuptools / setup-tools metadata
  spatium_supervisor/         Python package
    __init__.py
    __main__.py               CLI entrypoint — ``spatium-supervisor``
    config.py                 env-driven SupervisorConfig
    log.py                    structlog JSON setup
    state.py                  state-dir layout helpers
  images/supervisor/
    Dockerfile                multi-arch alpine image
    entrypoint.sh             tini-wrapped entry
  tests/                      pytest suite
```

## Local dev

```sh
cd agent/supervisor
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
spatium-supervisor                    # runs forever; SIGINT to stop
pytest                                # smoke tests
```

## Building the image

```sh
docker build -t spatium-supervisor:dev -f agent/supervisor/images/supervisor/Dockerfile .
```

(`context: .` from the repo root — the Dockerfile copies the package
from `agent/supervisor/`.)

[#170]: https://github.com/spatiumddi/spatiumddi/issues/170

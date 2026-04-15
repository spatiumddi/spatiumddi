# spatium-dns-agent

Sidecar agent baked into the SpatiumDDI managed BIND9 DNS container image
(`ghcr.io/spatiumddi/dns-bind9`).

See [`docs/deployment/DNS_AGENT.md`](../../docs/deployment/DNS_AGENT.md) for
the protocol specification.

## Environment

| Variable | Purpose |
|---|---|
| `CONTROL_PLANE_URL` | e.g. `https://api.spatiumddi.example` |
| `DNS_AGENT_KEY` | Bootstrap PSK, matches the control plane |
| `SERVER_NAME` | Hostname reported to the control plane |
| `AGENT_DRIVER` | `bind9` (only supported backend) |
| `AGENT_GROUP` | Optional DNS server group to join |
| `AGENT_ROLES` | Comma-separated: `authoritative,recursive,forwarder` |
| `TLS_CA_PATH` | Optional custom CA bundle |
| `SPATIUM_INSECURE_SKIP_TLS_VERIFY=1` | Dev only |

State directory: `/var/lib/spatium-dns-agent` (must be a volume).

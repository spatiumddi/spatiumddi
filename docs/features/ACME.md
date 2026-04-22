---
layout: default
title: ACME (DNS-01 provider)
---

# ACME DNS-01 Provider

SpatiumDDI ships an [acme-dns](https://github.com/joohoi/acme-dns)-compatible HTTP
surface under `/api/v1/acme/` so external ACME clients (certbot, lego,
acme.sh, etc.) can prove control of a FQDN hosted — or delegated to —
a SpatiumDDI-managed zone and issue public certs from Let's Encrypt
(or any other RFC 8555 CA).

**Who this is for**

- You own a public domain, and you want public certs (including
  wildcards) issued against it.
- Your DNS is either hosted directly on SpatiumDDI, or you can add a
  single CNAME in your existing registrar and delegate a small
  subzone to SpatiumDDI.
- You run the ACME client yourself — certbot / lego / acme.sh on
  whatever host needs the cert. SpatiumDDI does not issue certs; it
  answers the DNS challenge for you.

If you want SpatiumDDI to issue and auto-renew certs for **its own**
services (frontend HTTPS, DNS-over-TLS, etc.), that's a separate
embedded-client feature, still on the roadmap.

---

## How it works

DNS-01 is the only ACME challenge type that supports wildcards. The
client asks the CA for a cert, the CA says "prove you control this
domain by putting a specific TXT record at
`_acme-challenge.<domain>`", the client writes the record, the CA
polls DNS, if the record is there the cert is issued.

SpatiumDDI's role is only the "write the TXT record" half. The
acme-dns protocol shape decouples cert-issuing from DNS authority:

1. An operator registers an **ACME account** on SpatiumDDI. The
   account is bound to a specific DNS zone and gets a unique random
   subdomain label plus a username + password shown **once**.
2. The operator adds a CNAME in their *upstream* DNS provider:
   `_acme-challenge.<their-fqdn>` → `<subdomain>.<spatium-zone>`.
3. When the ACME client wants a cert, it hits SpatiumDDI's
   `/api/v1/acme/update` endpoint (authenticated with the
   credentials issued in step 1) and sets the TXT. SpatiumDDI writes
   the record through the normal DNS op pipeline and waits until the
   primary DNS server acknowledges it, so the CA's subsequent poll
   finds the record live.
4. The CA validates, issues the cert, the client cleans up by
   calling `DELETE /api/v1/acme/update`.

Security property: a leaked ACME credential can write TXT records
only at its one subdomain — it cannot rewrite the rest of the zone
or any other customer's data.

---

## Prerequisites

### 1. A SpatiumDDI-managed DNS zone to hold the TXT records

Create it like any other zone (DNS → Zones → **+ New Zone**). The
zone's FQDN is your choice; a common pattern is
`acme.<your-apex>.` (e.g. `acme.example.com.`). You'll delegate
**this subzone** to SpatiumDDI from your upstream registrar — the
rest of your zone can stay wherever it already lives.

The zone must have at least one **primary** DNS server attached
(server's `is_primary=True` flag). Record ops fan out from the
primary; without one, the server responds with 503 because it has
nowhere to write.

### 2. Delegation from your upstream registrar

If `example.com` lives on Route 53 (or Cloudflare, or whatever) and
you've created `acme.example.com.` on SpatiumDDI, add two records in
your upstream zone:

- `NS acme.example.com → ns1.spatium.yourdomain.com.`
- `NS acme.example.com → ns2.spatium.yourdomain.com.`

(Use however many NS records you have SpatiumDDI name servers.)

That's it — the delegation means upstream resolvers that look up
`<anything>.acme.example.com` will be pointed to SpatiumDDI.

### 3. A role with `acme_account` permission

Creating / listing / revoking ACME accounts is gated by the standard
RBAC. Superadmin bypasses. For a delegated admin, give a custom role
`admin` on `acme_account`:

```json
{
  "action": "admin",
  "resource_type": "acme_account"
}
```

The ACME **client** auth (username / password) is a separate
protocol and doesn't use SpatiumDDI roles at all.

---

## Worked example: certbot + ACME DNS-01 for `*.foo.example.com`

### Step 1 — Register an ACME account on SpatiumDDI

```bash
curl -X POST https://spatium.example.com/api/v1/acme/register \
  -H "Authorization: Bearer $SPATIUM_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "zone_id": "9f9b3c1e-0000-0000-0000-000000000000",
    "description": "certbot on foo.example.com",
    "allowed_source_cidrs": ["203.0.113.0/24"]
  }'
```

Response:

```json
{
  "username": "a1b2c3d4e5f6a7b8c9d0e1f2g3h4i5j6k7l8m9n0",
  "password": "o1p2q3r4s5t6u7v8w9x0y1z2a3b4c5d6e7f8g9h0",
  "subdomain": "6c48a6ed-8fa3-4f1d-b9ad-12f15a3e8c77",
  "fulldomain": "6c48a6ed-8fa3-4f1d-b9ad-12f15a3e8c77.acme.example.com",
  "allowfrom": ["203.0.113.0/24"]
}
```

**The `username` and `password` are shown exactly once.** Record
them now — SpatiumDDI stores only a bcrypt hash. If you lose them
you'll have to revoke the account and register a fresh one.

### Step 2 — Add the CNAME in your upstream registrar

In your public DNS zone for `foo.example.com` (upstream — the
registrar that *isn't* SpatiumDDI), create:

```
_acme-challenge.foo.example.com.   CNAME   6c48a6ed-8fa3-4f1d-b9ad-12f15a3e8c77.acme.example.com.
```

**Wildcard certs:** `_acme-challenge.example.com` (no leading
subdomain) covers both `example.com` and `*.example.com`. LE will
request two different validation tokens during wildcard issuance;
SpatiumDDI stores the two most-recent values at the label, so LE's
double-poll finds both simultaneously.

### Step 3 — Wire the credentials into your ACME client

**certbot** (via `certbot-dns-acmedns`):

```bash
pip install certbot certbot-dns-acmedns

# Creds file — one per account, 0600 perms
cat > /etc/letsencrypt/acmedns.json <<EOF
{
  "foo.example.com": {
    "username": "a1b2c3d4...",
    "password": "o1p2q3r4...",
    "fulldomain": "6c48a6ed-....acme.example.com",
    "subdomain": "6c48a6ed-...",
    "allowfrom": ["203.0.113.0/24"]
  }
}
EOF
chmod 600 /etc/letsencrypt/acmedns.json
```

Issue the cert:

```bash
certbot certonly \
  --authenticator dns-acmedns \
  --dns-acmedns-credentials /etc/letsencrypt/acmedns.json \
  --dns-acmedns-api-url https://spatium.example.com/api/v1/acme \
  -d foo.example.com -d '*.foo.example.com'
```

**lego** (supports acme-dns out of the box):

```bash
export ACME_DNS_API_BASE=https://spatium.example.com/api/v1/acme
export ACME_DNS_STORAGE_PATH=./acme-dns-accounts.json

lego --email you@example.com --dns acme-dns \
  -d foo.example.com -d '*.foo.example.com' run
```

Lego will prompt for the creds on first run and cache them in the
storage path for next time.

**acme.sh** (also native):

```bash
export ACMEDNS_BASE_URL="https://spatium.example.com/api/v1/acme"
export ACMEDNS_USERNAME="a1b2c3d4..."
export ACMEDNS_PASSWORD="o1p2q3r4..."
export ACMEDNS_SUBDOMAIN="6c48a6ed-..."

acme.sh --issue --dns dns_acmedns -d foo.example.com -d '*.foo.example.com'
```

### Step 4 — Auto-renewal

All three clients keep state across runs, so standard cron / systemd
timer setups (`certbot renew`, `lego renew`, `acme.sh --cron`) just
work. Renewals re-hit `/api/v1/acme/update` with fresh tokens; the
CNAME from step 2 stays in place forever.

---

## Protocol reference

All endpoints live under `/api/v1/acme/`.

### `POST /register`

**Auth:** SpatiumDDI JWT or API token; caller needs `write` on
`acme_account`.

**Request:**

```json
{
  "zone_id": "<uuid>",
  "description": "foo.example.com cert",
  "allowed_source_cidrs": ["203.0.113.0/24"]
}
```

**Response (201):**

```json
{
  "username": "<40-char>",
  "password": "<40-char, shown once>",
  "subdomain": "<uuid>",
  "fulldomain": "<subdomain>.<zone-fqdn>",
  "allowfrom": ["203.0.113.0/24"]
}
```

### `POST /update`

**Auth:** `X-Api-User` + `X-Api-Key` headers with the account's
username and password. No JWT.

**Request:**

```json
{
  "subdomain": "<subdomain>",
  "txt": "<43-char base64url validation token from the CA>"
}
```

**Response (200):**

```json
{"txt": "<same value echoed back>"}
```

**Blocking behaviour:** the endpoint blocks until the TXT write is
acknowledged by the zone's primary DNS server (default timeout
30 s). On healthy systems this typically resolves in 1-5 s. If the
agent is unreachable the endpoint returns 504; LE / ACME clients
retry automatically.

**Wildcard certs:** at most the two most recent values are kept at
the same subdomain. A third `/update` evicts the oldest. This
matches canonical acme-dns behaviour and is the reason the DNS-01
wildcard flow works at all (the `_acme-challenge.example.com` label
holds both validation tokens during a wildcard+base request).

**Idempotent retries:** posting the exact same `(subdomain, txt)`
pair a second time is a no-op and returns 200 immediately.

### `DELETE /update`

**Auth:** same as `POST /update`.

Clears every TXT record at the account's subdomain. Well-behaved
clients call this after validation. Records that aren't cleaned up
get swept automatically after 24 h by a Celery janitor.

### `GET /accounts`

**Auth:** `read` on `acme_account`.

Lists every ACME account, no credentials. Each row includes
`last_used_at` so operators can tell dead accounts from live ones.

### `DELETE /accounts/{id}`

**Auth:** `delete` on `acme_account`.

Hard-deletes the account and removes any TXT records it owns. Any
in-flight `/update` call using those credentials returns 401 on its
next attempt.

---

## Security and operational notes

- **Credential shape.** Username + password are each 40 chars of
  URL-safe random entropy (~240 bits each). Password is bcrypt-hashed
  at rest. The username is stored in plaintext since it's the
  lookup key.
- **Source IP allowlist.** `allowed_source_cidrs` is checked on every
  `/update` call against the HTTP client address. If you're behind a
  reverse proxy you'll want X-Forwarded-For trust configured on the
  proxy AND a known-good SpatiumDDI trusted-proxy list — without
  that, `allowed_source_cidrs` sees the proxy IP, not the real
  client. For unambiguous source gating, terminate TLS on SpatiumDDI
  directly or use network-level ACLs.
- **Rate limiting.** The v1 release does not ship a rate limiter
  specifically for `/api/v1/acme/`. Operators running the surface
  publicly should front it with a WAF or proxy-level rate limit.
  The `wait_for_op_applied` loop caps at 30 s so even an aggressive
  attacker can't tie up more than a fixed number of connections per
  second.
- **Audit log.** Every register / update / delete writes an
  `audit_log` entry. The TXT value is NEVER logged in full — only a
  12-char prefix for correlation. Credentials are never logged,
  hashed or otherwise.
- **Stale record sweep.** A Celery beat task clears any ACME-owned
  TXT records older than 24 h. Registered accounts are never
  auto-revoked — operators manage those manually.
- **Wildcard certs and delegation together.** The tiny subzone
  delegation means an attacker who steals one ACME credential from
  customer A cannot issue certs for customer B's names, even if both
  point at the same SpatiumDDI instance.

---

## Troubleshooting

- **Client says "DNS record not found" even though you just
  `/update`d.** The endpoint blocks until the *primary* ack returns,
  but your client might be polling a secondary/caching resolver that
  hasn't transferred yet. Check the zone's primary ↔ secondary sync
  (DNS → Zone → *Server state*). If primary shows applied but
  secondary doesn't, the delegation NS records in your upstream zone
  may not include both SpatiumDDI servers.
- **504 on `/update`.** Primary DNS server is unreachable from the
  control plane or the agent isn't heartbeating. Check
  *DNS → Servers → Status*.
- **401 on `/update` with known-good credentials.** Either the
  subdomain in the request body doesn't match the authenticated
  account (common copy-paste error), the source IP isn't in
  `allowed_source_cidrs`, or the account was revoked.
- **CA validation still fails after `/update` returns 200.** The TXT
  is live on SpatiumDDI's primary — check the CNAME chain from
  `_acme-challenge.<your-fqdn>` outward with `dig +trace` and verify
  the delegation NS records in your upstream zone point to
  SpatiumDDI.

---

## Roadmap (deferred from the MVP)

- Dedicated rate-limit bucket with fail2ban-style temp-ban on
  repeated `/update` auth failures.
- Per-op `asyncio.Event` signaling to replace the DB-polling wait
  (shaves ~250 ms off the typical `/update` latency).
- Janitor metrics exposed on the admin dashboard (sweep count,
  oldest-record-age).
- Pass-through of `allowfrom` enforcement to the CNAME-delegation
  side so upstream resolvers can prune queries they're not
  delegated.

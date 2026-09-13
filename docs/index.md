---
title: Documentation
description: SpatiumDDI documentation — setup, architecture, feature specs, deployment, driver internals and the privacy statement.
---


<div class="idx-lead" markdown="0">
  <h1>SpatiumDDI Documentation</h1>
  <p>
    SpatiumDDI is an open-source, self-hosted <strong>DNS, DHCP and IP Address
    Management</strong> platform. It does not merely configure external servers —
    it runs BIND9, PowerDNS, Technitium and Kea as managed service containers,
    with one control plane over all three.
  </p>
  <p class="idx-lead-note">
    These pages are the reference material: every specification, deployment
    guide and driver internal, grouped by what you are trying to do.
  </p>
  <div class="idx-actions">
    <a class="idx-btn idx-btn-primary" href="GETTING_STARTED.html">Get started</a>
    <a class="idx-btn" href="deployment/DOCKER.html">Deploy with Docker</a>
    <a class="idx-btn" href="ARCHITECTURE.html">How it fits together</a>
  </div>
</div>

<div class="idx-facts" markdown="0">
  <div class="idx-fact"><span class="idx-fact-k">DNS engines</span><span class="idx-fact-v">BIND9 · PowerDNS · Technitium · Windows · 4 clouds</span></div>
  <div class="idx-fact"><span class="idx-fact-k">DHCP engines</span><span class="idx-fact-v">Kea · Windows DHCP</span></div>
  <div class="idx-fact"><span class="idx-fact-k">Deploy as</span><span class="idx-fact-v">Compose · Kubernetes · OS appliance</span></div>
  <div class="idx-fact"><span class="idx-fact-k">Telemetry</span><span class="idx-fact-v"><a href="PRIVACY.html">None — ever</a></span></div>
</div>


<h2 class="idx-h2" id="start-here">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
  Start Here
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="GETTING_STARTED.html">
    <span class="idx-card-title">Getting Started</span>
    <span class="idx-card-desc">Recommended setup order (servers → zones/scopes → subnets → addresses)</span>
  </a>
  <a class="idx-card" href="TROUBLESHOOTING.html">
    <span class="idx-card-title">Troubleshooting</span>
    <span class="idx-card-desc">Recovery recipes: deleted agent rows, password reset, refused subnet deletes, and more</span>
  </a>
  <a class="idx-card" href="deployment/WINDOWS.html">
    <span class="idx-card-title">Windows Server Setup</span>
    <span class="idx-card-desc">WinRM, service accounts, firewall — Windows-side checklist for agentless DNS + DHCP</span>
  </a>
</div>


<h2 class="idx-h2" id="architecture-design">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 21h18M5 21V7l7-4 7 4v14M9 21v-6h6v6"/></svg>
  Architecture &amp; Design
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="ARCHITECTURE.html">
    <span class="idx-card-title">Architecture</span>
    <span class="idx-card-desc">System topology, component relationships, HA design</span>
  </a>
  <a class="idx-card" href="deployment/TOPOLOGIES.html">
    <span class="idx-card-title">Deployment Topologies</span>
    <span class="idx-card-desc">Six reference production layouts with sizing notes</span>
  </a>
  <a class="idx-card" href="DATA_MODEL.html">
    <span class="idx-card-title">Data Model</span>
    <span class="idx-card-desc">Database models, relationships, field definitions</span>
  </a>
  <a class="idx-card" href="API.html">
    <span class="idx-card-title">API Conventions</span>
    <span class="idx-card-desc">REST API conventions, pagination, error format</span>
  </a>
  <a class="idx-card" href="PERMISSIONS.html">
    <span class="idx-card-title">Permissions</span>
    <span class="idx-card-desc">RBAC grammar, builtin roles, scope delegation</span>
  </a>
  <a class="idx-card" href="OBSERVABILITY.html">
    <span class="idx-card-title">Observability</span>
    <span class="idx-card-desc">Logging, metrics, health dashboard</span>
  </a>
  <a class="idx-card" href="design/FLEET_FIREWALL.html">
    <span class="idx-card-title">Fleet Firewall design</span>
    <span class="idx-card-desc">Declarative per-role appliance firewall policy compiled to nftables</span>
  </a>
  <a class="idx-card" href="SHIPPED.html">
    <span class="idx-card-title">Shipped roadmap items</span>
    <span class="idx-card-desc">The full design context behind every feature that has landed</span>
  </a>
</div>


<h2 class="idx-h2" id="feature-specs">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h16v16H4zM8 8h8M8 12h8M8 16h5"/></svg>
  Feature Specs
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="features/IPAM.html">
    <span class="idx-card-title">IPAM</span>
    <span class="idx-card-desc">IP spaces, blocks, subnets, addresses, custom fields</span>
  </a>
  <a class="idx-card" href="features/DNS.html">
    <span class="idx-card-title">DNS</span>
    <span class="idx-card-desc">Zones, records, views, server groups, blocking lists, encrypted transports, Windows DNS (Path A + B)</span>
  </a>
  <a class="idx-card" href="features/DHCP.html">
    <span class="idx-card-title">DHCP</span>
    <span class="idx-card-desc">Servers, scopes, pools, static assignments, leases, Windows DHCP (Path A)</span>
  </a>
  <a class="idx-card" href="features/AUTH.html">
    <span class="idx-card-title">Auth &amp; Permissions</span>
    <span class="idx-card-desc">LDAP, OIDC, SAML, RADIUS, TACACS+, roles, API tokens</span>
  </a>
  <a class="idx-card" href="features/ACME.html">
    <span class="idx-card-title">ACME DNS-01 Provider</span>
    <span class="idx-card-desc">Acme-dns-compatible surface for LE / public-CA cert issuance against SpatiumDDI-managed zones</span>
  </a>
  <a class="idx-card" href="features/MIGRATION.html">
    <span class="idx-card-title">Migration</span>
    <span class="idx-card-desc">One-shot importers for BIND9 / Windows / PowerDNS / Technitium DNS, Kea / Windows / ISC DHCP, and NetBox IPAM, plus the guided Windows cutover</span>
  </a>
  <a class="idx-card" href="features/INTEGRATIONS.html">
    <span class="idx-card-title">Integrations</span>
    <span class="idx-card-desc">Read-only Kubernetes, Docker, Proxmox VE, Tailscale, cloud and firewall mirrors into IPAM; per-integration setup, mirror semantics, dashboard surface</span>
  </a>
  <a class="idx-card" href="features/LOOKING_GLASS.html">
    <span class="idx-card-title">BGP Looking Glass</span>
    <span class="idx-card-desc">Receive-only BGP collector (GoBGP) peering with the operator&#x27;s routers; Sessions + Routes grid, RPKI status at ingest</span>
  </a>
  <a class="idx-card" href="features/VERTICALS.html">
    <span class="idx-card-title">Vertical network awareness</span>
    <span class="idx-card-desc">AV-over-IP flow descriptors, BACnet/IP device-instance registry, Industrial-OT inventory + Purdue zoning, DICOM AE Title registry, and the fragile-device do-not-probe flag</span>
  </a>
  <a class="idx-card" href="features/E911.html">
    <span class="idx-card-title">E911 dispatchable location</span>
    <span class="idx-card-desc">SpatiumDDI as a Location Information Server: Emergency Response Locations as RFC 5139 civic addresses, switch-port / subnet / VLAN / device bindings, HELD and DHCP option delivery</span>
  </a>
  <a class="idx-card" href="features/SYSTEM_ADMIN.html">
    <span class="idx-card-title">System Admin</span>
    <span class="idx-card-desc">Config, health dashboard, notifications, backup/restore, service control</span>
  </a>
</div>


<h2 class="idx-h2" id="deployment">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 16V8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"/></svg>
  Deployment
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="deployment/DOCKER.html">
    <span class="idx-card-title">Docker Compose</span>
    <span class="idx-card-desc">Quick start, profiles, TLS, HA</span>
  </a>
  <a class="idx-card" href="deployment/WINDOWS.html">
    <span class="idx-card-title">Windows Server</span>
    <span class="idx-card-desc">Connecting to Windows DNS / DHCP over WinRM + RFC 2136</span>
  </a>
  <a class="idx-card" href="deployment/KUBERNETES.html">
    <span class="idx-card-title">Kubernetes</span>
    <span class="idx-card-desc">Umbrella Helm chart, HPA, Ingress / LoadBalancer, CloudNativePG + Redis Sentinel HA</span>
  </a>
  <a class="idx-card" href="deployment/BAREMETAL.html">
    <span class="idx-card-title">Bare Metal</span>
    <span class="idx-card-desc">Docker Compose on a host, Patroni HA Postgres overlay, OS appliance path</span>
  </a>
  <a class="idx-card" href="deployment/APPLIANCE.html">
    <span class="idx-card-title">OS Appliance</span>
    <span class="idx-card-desc">Appliance image build, installer, A/B slot upgrades</span>
  </a>
  <a class="idx-card" href="deployment/DNS_AGENT.html">
    <span class="idx-card-title">DNS Agent</span>
    <span class="idx-card-desc">Agent protocol, auto-registration, config sync</span>
  </a>
</div>


<h2 class="idx-h2" id="driver-internals">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16M8 3v4M16 10v4M8 14v6"/></svg>
  Driver Internals
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="drivers/DNS_DRIVERS.html">
    <span class="idx-card-title">DNS Drivers</span>
    <span class="idx-card-desc">BIND9 + PowerDNS + Technitium + Windows DNS driver internals</span>
  </a>
  <a class="idx-card" href="drivers/DHCP_DRIVERS.html">
    <span class="idx-card-title">DHCP Drivers</span>
    <span class="idx-card-desc">Kea + Windows DHCP driver internals</span>
  </a>
</div>


<h2 class="idx-h2" id="development-testing">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6L2 12l6 6M16 6l6 6-6 6"/></svg>
  Development &amp; Testing
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="DEVELOPMENT.html">
    <span class="idx-card-title">Development Guide</span>
    <span class="idx-card-desc">Coding standards, test requirements, CI</span>
  </a>
  <a class="idx-card" href="PERFORMANCE_TESTING.html">
    <span class="idx-card-title">Performance Testing</span>
    <span class="idx-card-desc">The 24-hour university-scale load and soak plan behind the <code>perf/</code> suite</span>
  </a>
  <a class="idx-card" href="PERF_APPLIANCE_SETUP.html">
    <span class="idx-card-title">Performance-test appliance setup</span>
    <span class="idx-card-desc">Preparing a clean single-node appliance for the suite to drive</span>
  </a>
</div>


<h2 class="idx-h2" id="project">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z"/></svg>
  Project
</h2>

<div class="idx-grid" markdown="0">
  <a class="idx-card" href="PRIVACY.html">
    <span class="idx-card-title">Privacy</span>
    <span class="idx-card-desc">No telemetry, no analytics, no phone-home; every outbound connection the software can make, what it sends, and how to turn it off</span>
  </a>
  <a class="idx-card" href="THIRD_PARTY.html">
    <span class="idx-card-title">Third-Party Components</span>
    <span class="idx-card-desc">Every bundled engine, library and OS package, with licenses and which artifact each ships in</span>
  </a>
  <a class="idx-card" href="https://github.com/spatiumddi/spatiumddi">
    <span class="idx-card-title">Source, releases and issues</span>
    <span class="idx-card-desc">The project on GitHub</span>
  </a>
</div>


<p class="idx-foot">
  Source for these pages lives in the
  <a href="https://github.com/spatiumddi/spatiumddi/tree/main/docs"><code>docs/</code> directory</a>
  of the main repository and is republished on every push to <code>main</code>.
</p>

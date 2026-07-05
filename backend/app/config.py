import sys

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel default for ``secret_key``. Production deployments MUST
# override this via the ``SECRET_KEY`` env var (or ``.env``) — the
# model validator below emits a loud stderr warning whenever the
# sentinel is in use, and hard-fails the boot when
# ``STRICT_SECRET_KEY=true`` is set (recommended for any non-dev
# deployment).
_SECRET_KEY_DEV_SENTINEL = "change-me-to-a-random-32-char-string"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://spatiumddi:changeme@postgres:5432/spatiumddi"
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = "redis://redis:6379/0"
    # Redis Sentinel (#272 Phase 3). When ``redis_url`` /
    # ``celery_broker_url`` carry a ``sentinel://`` scheme, the
    # connection helpers query Sentinel for the current master named
    # ``redis_sentinel_master``. ``redis_sentinel_password`` is the
    # password Sentinel itself requires (often the same as the data
    # password); empty falls back to the password embedded in the URL.
    redis_sentinel_master: str = "mymaster"
    redis_sentinel_password: str = ""

    # Security
    secret_key: str = _SECRET_KEY_DEV_SENTINEL
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    credential_encryption_key: str = ""

    # CORS — comma-separated allowed origins (e.g.
    # "https://ddi.example.com,https://ipam.example.com"). Default "*"
    # keeps dev / same-origin appliance deployments working out of the
    # box. When left as "*" we serve a wildcard WITHOUT
    # ``allow_credentials`` — that avoids the dangerous "reflect any origin
    # + allow credentials" combo Starlette produces for ``["*"]`` +
    # ``allow_credentials=True``. Set explicit origins to lock the API
    # down to your frontend(s); credentials are then enabled for them.
    #
    # NOTE (#484): API calls authenticate via the Bearer Authorization
    # header, but the auth endpoints under ``/api/v1/auth`` also set/read the
    # HttpOnly ``spatium_refresh`` cookie. That cookie only matters for a
    # cross-ORIGIN frontend, which requires explicit ``cors_origins`` anyway
    # (wildcard mode disables credentials); the default same-origin
    # (nginx-proxied) deployment needs no CORS credentials at all.
    #
    # SECURITY (#400 / M6): if ``"*"`` appears MIXED with explicit
    # origins (e.g. ``"*,https://app.example.com"``) we treat the whole
    # list as a wildcard (``cors_origins_list`` collapses to ``["*"]``),
    # which forces ``allow_credentials=False`` at the middleware. Without
    # this, the explicit entries would flip credentials on while the
    # ``"*"`` still reflected every Origin — the exact "trust any site +
    # send credentials" combo we guard against above.
    cors_origins: str = "*"

    # TrustedHostMiddleware allow-list (#400 / L3). Comma-separated host
    # patterns (e.g. "ddi.example.com,*.example.com"). Default "*"
    # accepts any Host header so existing deploys behind a trusted
    # reverse proxy / on the appliance keep working out of the box.
    # Operators who terminate TLS directly on the API (or want defence
    # against Host-header injection / DNS-rebinding / cache-poisoning)
    # set this to their real hostnames. Starlette's TrustedHostMiddleware
    # treats ``["*"]`` as "allow everything".
    trusted_hosts: str = "*"

    # External auth providers (LDAP / OIDC / SAML) are configured via the GUI at
    # /admin/auth-providers — see backend/app/models/auth_provider.py. Secrets are
    # encrypted with the Fernet helper in app.core.crypto using
    # credential_encryption_key (or falling back to secret_key).

    # Celery
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Features
    feature_discovery_enabled: bool = True
    feature_dhcp_enabled: bool = True
    feature_dns_enabled: bool = True

    # Observability
    log_level: str = "INFO"
    log_format: str = "json"
    prometheus_metrics_enabled: bool = True

    # DNS agent
    dns_agent_key: str = ""
    dns_agent_token_ttl_hours: int = 24
    dns_agent_longpoll_timeout: int = 30
    dns_require_agent_approval: bool = False

    # DHCP agent
    dhcp_agent_key: str = ""
    dhcp_agent_token_ttl_hours: int = 24
    dhcp_agent_longpoll_timeout: int = 30

    # BGP Looking Glass collector agent (#566)
    lg_agent_key: str = ""
    dhcp_require_agent_approval: bool = False
    dhcp_sync_interval_seconds: int = 60
    dhcp_lease_sync_interval_minutes: int = 5

    # App
    app_title: str = "SpatiumDDI"
    debug: bool = False
    # Running version. Populated from the ``VERSION`` env var, which the
    # compose file threads in from ``SPATIUMDDI_VERSION`` (same value the
    # operator sets to pick which image tag to run). Falls back to ``dev``
    # so unversioned local builds are obvious in the sidebar and don't
    # misreport themselves as a release.
    version: str = "dev"

    # GitHub repo coordinates used by the release-check task. Overridable
    # so forks can point their update check at their own repo.
    github_repo: str = "spatiumddi/spatiumddi"

    # When ``True`` (recommended for any non-dev deployment), the boot
    # fails fast if ``SECRET_KEY`` is still set to the .env.example
    # sentinel. Default ``False`` so a fresh ``cp .env.example .env``
    # first-time setup boots and the operator can log in to fix it,
    # but every boot with the sentinel still emits a loud stderr
    # warning regardless of this flag. See #216.
    strict_secret_key: bool = False

    # #565 — when ``True``, the Celery worker refuses to process tasks
    # while the DB schema is behind the bundled Alembic head (mirrors
    # ``strict_secret_key``'s opt-in shape). Default ``False`` so a
    # transient mid-rollout window (code up before ``alembic upgrade
    # head`` finishes) doesn't hard-stop the worker — the schema drift
    # is logged loudly + raised as an ``AlertEvent`` regardless. Set to
    # ``True`` in environments where a stale-schema task run is worse
    # than a deferred one.
    strict_schema_check: bool = False

    # #296 Phase B — slot-image mirror config.
    #
    # ``slot_image_mirror_url`` — when set, the slot-image upload +
    # download + delete handlers stream byte ops to this URL instead
    # of touching local FS. The mirror is a single-replica Deployment
    # with a node-pinned local-path PVC (see
    # charts/spatiumddi/templates/slot-image-mirror.yaml). Empty on
    # docker-compose / single-instance shapes — the api keeps its
    # current "write to /var/lib/spatiumddi/slot-images" behaviour
    # via the existing named volume.
    slot_image_mirror_url: str = ""
    # ``slot_image_mirror_secret`` — shared HMAC secret between the
    # api and the mirror. The api adds an ``X-Mirror-Auth`` header on
    # every internal call; the mirror verifies it before serving any
    # byte op. Defence in depth alongside the in-cluster Service
    # isolation. Auto-populated from the chart-rendered k8s Secret.
    slot_image_mirror_secret: str = ""
    # ``slot_image_mirror_mode`` — set on the mirror Deployment only.
    # Flips the api process into "I am the byte store" mode so the
    # ``/internal/slot-images`` endpoints accept X-Mirror-Auth
    # traffic. Leave false on every other api Deployment.
    slot_image_mirror_mode: bool = False

    # Demo mode — locks down abusable mutation surfaces (nmap, AI
    # provider creates, webhook targets, integration targets,
    # outbound mail/audit/backup, factory reset, password change)
    # and force-disables a curated set of feature modules. Used by
    # the GitHub Codespaces public demo so a visitor can't weaponise
    # it as a scanner / SSRF / relay. See app.core.demo_mode.
    demo_mode: bool = False

    # Appliance mode — set true by the appliance ISO compose env so
    # the API knows it's running on an appliance image (vs. plain
    # docker-compose / k8s). Gates the "Appliance" sidebar section
    # and the /api/v1/appliance/* router family that surfaces
    # appliance-only management (TLS cert upload, release manager,
    # container live-logs, host network config, maintenance mode,
    # diagnostic bundle download). Phase 4 — see issue #134.
    #
    # appliance_version and appliance_hostname are populated by
    # spatiumddi-firstboot at install time so the management UI can
    # render "SpatiumDDI Appliance v0.1.0 @ host-name" without an
    # extra round-trip into the OS.
    appliance_mode: bool = False
    appliance_version: str = ""
    appliance_hostname: str = ""
    # Comma-separated list of the host's real IPv4/IPv6 addresses,
    # detected by spatiumddi-firstboot via `ip -o addr show scope global`
    # and threaded through .env. Used by the self-signed cert bootstrap
    # so the SAN list carries the IPs a browser will see (the api
    # container's own socket-level view only knows the docker bridge IP).
    # Empty on non-appliance deploys.
    appliance_host_ips: str = ""
    # #272 Phase 6 — extra SAN entries the self-signed cert MUST cover
    # beyond the host's own IPs: the control-plane VIP (and any other
    # floating address the operator's browser / agents hit). The
    # umbrella chart threads ``frontend.controlPlaneVIP`` in here on
    # promote; when set, the self-signed bootstrap regenerates an
    # existing self-signed cert that doesn't already cover these, so a
    # cert served on the VIP validates. Comma-separated IPs or DNS
    # names. Empty on single-node / non-appliance deploys.
    appliance_extra_cert_sans: str = ""

    # Where the cert deployer (Phase 4b.2) writes the currently-active
    # TLS cert + key. Mounted as a shared volume between the api
    # container (writes) and the appliance frontend nginx container
    # (reads from the same path on its side, conventionally
    # /etc/nginx/certs). On dev / non-appliance deploys the directory
    # may not exist; the deployer no-ops gracefully in that case.
    appliance_cert_dir: str = "/var/lib/spatiumddi/certs"
    # Name (or label) of the frontend container the deployer signals
    # SIGHUP to when a new cert is activated, so nginx reloads its
    # TLS context. Matches the compose service name on the appliance.
    appliance_frontend_container: str = "spatiumddi-frontend"

    # BGP prefix-hijack monitoring (issue #527). The periodic RIPEstat
    # poll (``app.tasks.bgp_hijack_poll``) is the reliable source of
    # truth and runs on any standard Celery deployment — gated by the
    # ``PlatformSettings.bgp_monitoring_enabled`` DB toggle, not by env.
    # This flag opts into the OPTIONAL long-running RIS Live WebSocket
    # consumer (``python -m app.services.bgp.ris_live``), the real-time
    # upgrade. Default OFF — a persistent WS doesn't fit the worker
    # model and the feature never depends on it.
    bgp_ris_live_enabled: bool = False
    bgp_ris_live_url: str = "wss://ris-live.ripe.net/v1/ws/"

    @property
    def cors_origins_list(self) -> list[str]:
        """``cors_origins`` parsed into a list. ``"*"`` (the default)
        yields ``["*"]``; a comma-separated value yields the trimmed,
        non-empty entries.

        SECURITY (#400 / M6): if ``"*"`` is present ANYWHERE in the list
        (even mixed with explicit origins), the whole list collapses to
        ``["*"]``. The middleware keys ``allow_credentials`` off this
        result being exactly ``["*"]``, so a wildcard always disables
        credentials regardless of any explicit entries the operator
        also listed. This closes the "explicit origin enables creds while
        ``*`` still reflects every Origin" combo.
        """
        raw = self.cors_origins.strip()
        if raw == "*" or not raw:
            return ["*"]
        entries = [o.strip() for o in raw.split(",") if o.strip()]
        if "*" in entries:
            return ["*"]
        return entries

    @property
    def trusted_hosts_list(self) -> list[str]:
        """``trusted_hosts`` parsed into a list for TrustedHostMiddleware
        (#400 / L3). ``"*"`` (the default) yields ``["*"]`` — accept any
        Host header; a comma-separated value yields the trimmed,
        non-empty patterns."""
        raw = self.trusted_hosts.strip()
        if raw == "*" or not raw:
            return ["*"]
        entries = [h.strip() for h in raw.split(",") if h.strip()]
        if "*" in entries:
            return ["*"]
        return entries or ["*"]

    @model_validator(mode="after")
    def _check_secret_key_default(self) -> "Settings":
        # The sentinel JWT-signing key in ``.env.example`` MUST NOT
        # reach a production deploy. Emit a loud warning every boot;
        # hard-fail when ``STRICT_SECRET_KEY=true`` is set.
        if self.secret_key == _SECRET_KEY_DEV_SENTINEL:
            if self.strict_secret_key:
                raise ValueError(
                    "SECRET_KEY is still set to the .env.example default and "
                    "STRICT_SECRET_KEY=true. Generate a real key with "
                    "`openssl rand -hex 32` and set SECRET_KEY in .env."
                )
            # Skip the stderr spam under pytest — Settings() is built at
            # import time, so the sentinel (which every test DB uses) would
            # print on every collection. ``pytest`` is always in
            # sys.modules by the time the app imports during a test run.
            if "pytest" not in sys.modules:
                print(
                    "WARNING: SECRET_KEY is still the .env.example sentinel. "
                    "Set SECRET_KEY=<openssl rand -hex 32> in .env. "
                    "Enable STRICT_SECRET_KEY=true in non-dev deployments to "
                    "make this a hard error.",
                    file=sys.stderr,
                    flush=True,
                )
        return self


settings = Settings()

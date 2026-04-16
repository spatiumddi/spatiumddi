from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://spatiumddi:changeme@postgres:5432/spatiumddi"
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Security
    secret_key: str = "change-me-to-a-random-32-char-string"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    credential_encryption_key: str = ""

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
    dhcp_require_agent_approval: bool = False
    dhcp_sync_interval_seconds: int = 60
    dhcp_lease_sync_interval_minutes: int = 5

    # App
    app_title: str = "SpatiumDDI"
    debug: bool = False


settings = Settings()

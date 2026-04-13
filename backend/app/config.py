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

    # Auth: LDAP
    ldap_enabled: bool = False
    ldap_host: str = ""
    ldap_port: int = 636
    ldap_use_ssl: bool = True
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_group_base_dn: str = ""

    # Auth: OIDC
    oidc_enabled: bool = False
    oidc_provider_name: str = ""
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""

    # Celery
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Features
    feature_discovery_enabled: bool = True
    feature_dhcp_enabled: bool = True
    feature_dns_enabled: bool = True
    feature_ntp_enabled: bool = True

    # Observability
    log_level: str = "INFO"
    log_format: str = "json"
    prometheus_metrics_enabled: bool = True

    # App
    app_title: str = "SpatiumDDI"
    debug: bool = False


settings = Settings()

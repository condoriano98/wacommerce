from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every secret comes from the environment, never from the repository."""

    model_config = SettingsConfigDict(env_prefix="OMS_", env_file=".env", extra="ignore")

    env: str = "dev"
    # Application role: subject to row-level security (no BYPASSRLS).
    database_url: str = "postgresql+psycopg://oms_app:oms_app@localhost:5432/oms"
    # ADR-014 backward-compatible cutover: "legacy" = V3.0 BYPASSRLS oms_system role for worker/webhooks;
    # "v31" = oms_worker / oms_webhook roles subject to RLS (per-org system principals + definer resolvers).
    db_role_mode: str = "v31"
    worker_database_url: str = "postgresql+psycopg://oms_worker:oms_worker@localhost:5432/oms"
    webhook_database_url: str = "postgresql+psycopg://oms_webhook:oms_webhook@localhost:5432/oms"
    # Legacy only (removed by the contract migration).
    system_database_url: str = "postgresql+psycopg://oms_system:oms_system@localhost:5432/oms"
    # Owner role used by Alembic migrations.
    migration_database_url: str = "postgresql+psycopg://postgres@localhost:5432/oms"

    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_minutes: int = 480
    # Fernet key (urlsafe base64, 32 bytes) for platform credentials.
    credentials_master_key: str = "q4nJ0l0Yk5Dk0c8h3mGQx1u8y0dZ8C0lV3m6f0QzJ7k="

    meta_graph_version: str = "v27.0"
    meta_app_secret: str = "dev-meta-app-secret"
    meta_verify_token: str = "dev-verify-token"
    chatwoot_base_url: str = "http://chatwoot.local"
    # HMAC secret for Chatwoot API-channel callbacks (X-Chatwoot-Signature over "{timestamp}.{body}").
    chatwoot_webhook_secret: str = "dev-chatwoot-webhook-secret"

    outbox_max_attempts: int = 8
    capi_max_event_age_days: int = 7
    attribution_lookback_days: int = 7
    insights_repull_days: int = 28


@lru_cache
def get_settings() -> Settings:
    return Settings()

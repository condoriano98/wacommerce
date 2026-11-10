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
    credentials_previous_keys: str = ""  # comma-separated, decrypt-only during a key rotation

    meta_graph_version: str = "v27.0"
    meta_app_secret: str = "dev-meta-app-secret"
    meta_verify_token: str = "dev-verify-token"
    chatwoot_base_url: str = "http://chatwoot.local"
    # HMAC secret for Chatwoot API-channel callbacks (X-Chatwoot-Signature over "{timestamp}.{body}").
    chatwoot_webhook_secret: str = "dev-chatwoot-webhook-secret"
    # DOKU notification signature secret (merchant secret key; from the secret store in deployed environments).
    doku_notification_secret: str = "dev-doku-secret"
    doku_notification_path: str = "/webhooks/doku"
    # Biteship webhook authentication is unverified (G-BS2): the callback URL carries a shared secret token
    # (``/webhooks/biteship?token=…``, configured in the Biteship dashboard). Empty → endpoint refuses all callbacks.
    biteship_webhook_token: str = ""

    outbox_max_attempts: int = 8
    # Risk-alert notification channels (comma separated). Only "mock" exists until a live channel is authorised.
    risk_alert_channels: str = "mock"
    notify_max_attempts: int = 4
    # Bearer token for /metrics (Prometheus). Empty → endpoint disabled (404).
    metrics_token: str = ""
    # Webhook replay window (seconds) for providers that sign a timestamp (Chatwoot, DOKU).
    webhook_max_age_seconds: int = 900
    capi_max_event_age_days: int = 7
    attribution_lookback_days: int = 7
    insights_repull_days: int = 28


DEV_DEFAULTS = {
    "jwt_secret": "dev-only-change-me",
    "credentials_master_key": "q4nJ0l0Yk5Dk0c8h3mGQx1u8y0dZ8C0lV3m6f0QzJ7k=",
    "meta_app_secret": "dev-meta-app-secret",
    "meta_verify_token": "dev-verify-token",
    "chatwoot_webhook_secret": "dev-chatwoot-webhook-secret",
    "doku_notification_secret": "dev-doku-secret",
}


def production_problems(s: "Settings") -> list[str]:
    """Settings that must never reach production: dev defaults or empty secrets (checked at process start)."""
    if s.env not in ("production", "staging"):
        return []
    problems = [f"{k} uses the development default" for k, v in DEV_DEFAULTS.items() if getattr(s, k) == v]
    if len(s.jwt_secret) < 32:
        problems.append("jwt_secret shorter than 32 characters")
    if not s.biteship_webhook_token:
        problems.append("biteship_webhook_token is empty")
    for url in ("database_url", "worker_database_url", "webhook_database_url"):
        if ":oms_app@" in getattr(s, url) or ":oms_worker@" in getattr(s, url) or ":oms_webhook@" in getattr(s, url):
            problems.append(f"{url} uses the development password")
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()

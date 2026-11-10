"""Adapter selection (ADR-004): mock unless the platform account is live-enabled AND has credentials."""

from app.integrations.chatwoot import LiveChatwoot, MockChatwoot
from app.integrations.whatsapp import LiveWhatsAppSender, MockWhatsAppSender
from app.models.platforms import PlatformAccount
from app.modules.platforms import read_secret

MOCK_WHATSAPP = MockWhatsAppSender()
MOCK_CHATWOOT = MockChatwoot()


def whatsapp_sender(account: PlatformAccount):
    if account.live_enabled:
        token = read_secret(account.organization_id, account.id, "access_token")
        if token:
            return LiveWhatsAppSender(token, account.api_version)
    return MOCK_WHATSAPP


def chatwoot_client(account: PlatformAccount | None):
    if account is not None and account.live_enabled:
        token = read_secret(account.organization_id, account.id, "api_token")
        cfg = account.public_config
        if token:
            return LiveChatwoot(cfg["base_url"], int(cfg["account_id"]), token, int(cfg["inbox_id"]))
    return MOCK_CHATWOOT


def payment_provider(account: PlatformAccount | None):
    from app.integrations.doku import LiveDoku, MockDoku

    if account is not None and account.live_enabled:
        secret = read_secret(account.organization_id, account.id, "secret_key")
        if secret:
            return LiveDoku(account.public_config["client_id"], secret,
                            account.public_config.get("base_url", "https://api-sandbox.doku.com"))
    return MockDoku()


def carrier(account: PlatformAccount | None):
    from app.integrations.biteship import LiveBiteship, MockBiteship

    if account is not None and account.live_enabled:
        key = read_secret(account.organization_id, account.id, "api_key")
        if key:
            return LiveBiteship(key)
    return MockBiteship()

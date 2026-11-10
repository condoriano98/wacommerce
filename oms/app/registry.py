"""Import modules that register event subscribers and outbox handlers. Imported by the API and the worker."""

from app.modules import (  # noqa: F401
    conversions,
    fulfillment,
    messaging,
    orders,
    payments,
    procurement,
    provider_webhooks,
    settlements,
)
from app.modules.fpa import risk  # noqa: F401,E402

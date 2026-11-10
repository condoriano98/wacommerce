"""In-transaction domain events. Subscribers (conversions, reconciliation expectations, routing jobs) run in the same
transaction as the change that emitted the event, so effects are atomic with the business fact."""

from collections.abc import Callable

from sqlalchemy.orm import Session

Subscriber = Callable[[Session, dict], None]
_SUBSCRIBERS: dict[str, list[Subscriber]] = {}

EVENTS = (
    "order_confirmed",          # data: order
    "payment_captured",         # order, payment
    "payment_bank_matched",     # order, payment
    "payment_released",         # order, payment, basis (captured | bank_matched | attested_policy | cod | marketplace)
    "shipment_picked",          # order, shipment
    "shipment_delivered",       # order, shipment
    "shipment_returned",        # order, shipment
    "items_canceled",           # order, items
    "order_returned",           # order
)


def subscribe(event: str) -> Callable[[Subscriber], Subscriber]:
    if event not in EVENTS:
        raise ValueError(f"unknown event {event}")

    def register(fn: Subscriber) -> Subscriber:
        _SUBSCRIBERS.setdefault(event, []).append(fn)
        return fn

    return register


def emit(session: Session, event: str, **data) -> None:
    for fn in _SUBSCRIBERS.get(event, []):
        fn(session, data)

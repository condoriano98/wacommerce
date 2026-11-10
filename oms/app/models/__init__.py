"""Import every model module so Alembic and the app see the full metadata."""

from app.models import (  # noqa: F401
    catalog,
    customers,
    finance,
    fulfillment,
    governance,
    inventory,
    marketing,
    orders,
    org,
    platforms,
)
from app.models.base import Base

metadata = Base.metadata

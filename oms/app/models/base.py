import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(18, 2)
QTY = Numeric(18, 3)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        dict: JSONB,
        list: JSONB,
        Decimal: MONEY,
        datetime: DateTime(timezone=True),
        str: String,
    }

    def __init__(self, **kw):
        # Apply Python-side column defaults at construction time so ids, timestamps and statuses are
        # usable before flush (services link rows by id inside one transaction).
        cls = type(self)
        for col in cls.__table__.columns:
            if col.key in kw or col.default is None:
                continue
            if col.default.is_callable:
                kw[col.key] = col.default.arg(None)
            elif col.default.is_scalar:
                kw[col.key] = col.default.arg
        for key, value in kw.items():
            if not hasattr(cls, key):
                raise TypeError(f"{key!r} is an invalid keyword argument for {cls.__name__}")
            setattr(self, key, value)


class IdMixin:
    # Client-side ids and timestamps: inserts never need RETURNING, which matters under RLS
    # (a freshly inserted order is not visible until its lines exist).
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OrgMixin(IdMixin):
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)

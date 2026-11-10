"""Customer identity resolution (BSUID-first, phone optional, scoped to one business/brand)."""

import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.models.base import utcnow
from app.models.customers import Customer, CustomerIdentity, CustomerPortfolio, PrivacyPreference


def resolve_customer(session: Session, org_id: uuid.UUID, brand_id: uuid.UUID, identities: dict[str, str],
                     portfolio_ids: list[uuid.UUID], display_name: str | None = None) -> Customer:
    """Find the customer by any known identifier within this brand, attach new identifiers, link portfolios.

    Two existing customers are merged only when *the same webhook* proves both identifiers belong to one person
    (e.g. a message carrying both BSUID and phone). Names are never used for matching."""
    identities = {k: v for k, v in identities.items() if v}
    pairs = list(identities.items())
    found = session.scalars(select(CustomerIdentity).where(
        CustomerIdentity.brand_id == brand_id,
        tuple_(CustomerIdentity.identity_type, CustomerIdentity.identity_value).in_(pairs))).all() if pairs else []
    customer_ids = list(dict.fromkeys(i.customer_id for i in found))
    if not customer_ids:
        customer = Customer(organization_id=org_id, brand_id=brand_id, display_name=display_name)
        session.add(customer)
        session.flush()
    else:
        customers = sorted((session.get(Customer, cid) for cid in customer_ids), key=lambda c: c.created_at)
        customer = customers[0]
        while customer.merged_into_customer_id:
            customer = session.get(Customer, customer.merged_into_customer_id)
        for dup in customers[1:]:
            if dup.id == customer.id:
                continue
            dup.merged_into_customer_id = customer.id
            for ident in session.scalars(select(CustomerIdentity).where(CustomerIdentity.customer_id == dup.id)):
                ident.customer_id = customer.id
            for link in session.scalars(select(CustomerPortfolio).where(CustomerPortfolio.customer_id == dup.id)):
                portfolio_ids = [*portfolio_ids, link.portfolio_id]
            audit(session, org_id, "customer.merged", "customer", dup.id, actor_type="system",
                  after={"merged_into": customer.id, "evidence": list(identities)})
        if display_name and not customer.display_name:
            customer.display_name = display_name
    known = {(i.identity_type, i.identity_value) for i in found}
    for itype, value in pairs:
        if (itype, value) not in known:
            session.add(CustomerIdentity(organization_id=org_id, customer_id=customer.id, brand_id=brand_id,
                                         identity_type=itype, identity_value=value, first_seen_at=utcnow()))
    linked = set(session.scalars(select(CustomerPortfolio.portfolio_id).where(
        CustomerPortfolio.customer_id == customer.id)))
    for pid in dict.fromkeys(portfolio_ids):
        if pid not in linked:
            session.add(CustomerPortfolio(organization_id=org_id, customer_id=customer.id, portfolio_id=pid))
    session.flush()
    return customer


def consent_status(session: Session, customer_id: uuid.UUID, purpose: str) -> str:
    pref = session.scalars(select(PrivacyPreference).where(
        PrivacyPreference.customer_id == customer_id, PrivacyPreference.purpose == purpose)).first()
    return pref.consent_status if pref else "unknown"


def record_consent(session: Session, org_id: uuid.UUID, customer_id: uuid.UUID, purpose: str, decision: str,
                   *, notice_version: str, source: str, source_message_id: str | None = None,
                   lawful_basis: str = "consent") -> None:
    from app.models.customers import ConsentEvent

    session.add(ConsentEvent(organization_id=org_id, customer_id=customer_id, purpose=purpose, decision=decision,
                             notice_version=notice_version, source=source, source_message_id=source_message_id))
    pref = session.scalars(select(PrivacyPreference).where(
        PrivacyPreference.customer_id == customer_id, PrivacyPreference.purpose == purpose)).first()
    now = utcnow()
    if pref is None:
        pref = PrivacyPreference(organization_id=org_id, customer_id=customer_id, purpose=purpose,
                                 lawful_basis=lawful_basis, consent_status=decision)
        session.add(pref)
    pref.consent_status = decision
    if decision == "granted":
        pref.consent_timestamp, pref.withdrawal_timestamp = now, None
    elif decision == "withdrawn":
        pref.withdrawal_timestamp = now

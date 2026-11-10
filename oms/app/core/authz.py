"""Action permissions. *Visibility* of rows is enforced by Postgres RLS; this module decides which
*actions* a role may take within the scopes it can see."""

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, status

PERMISSIONS: dict[str, set[str]] = {
    "owner": {"*"},
    "portfolio_manager": {
        "portfolio.read", "portfolio.manage", "budget.manage", "catalog.manage", "orders.read",
        "orders.manage", "inventory.manage", "procurement.approve", "approvals.decide", "refund.approve",
        "pricing.manage", "fpa.read", "exceptions.manage", "marketing.read",
    },
    "ads_manager": {"marketing.read", "marketing.manage", "fpa.read", "portfolio.read"},
    "finance_manager": {
        "fpa.read", "finance.manage", "refund.approve", "approvals.decide", "orders.read", "settlements.manage",
        "exceptions.manage", "portfolio.read", "capital.approve",
    },
    "procurement_admin": {"procurement.operate", "orders.read", "catalog.read", "portfolio.read"},
    "packing_admin": {"fulfillment.operate", "orders.read", "inventory.read"},
    "cs_admin": {"orders.read", "customers.read", "conversations.manage", "refund.request", "exceptions.manage"},
    "auditor": {"portfolio.read", "orders.read", "fpa.read", "marketing.read", "audit.read", "customers.read"},
    "ai_sales_agent": {"agent.sales"},
    # Per-org system principal used by worker jobs (provider syncs, settlement ingestion). No approvals, no
    # credentials management, no capital: those stay human (spec §14).
    "system_worker": {"orders.read", "orders.manage", "finance.manage", "settlements.manage", "marketing.manage",
                      "fulfillment.operate", "inventory.read", "fpa.read"},
}


@dataclass
class Assignment:
    role: str
    scope_type: str
    scope_id: uuid.UUID | None


@dataclass
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    is_service: bool = False
    assignments: list[Assignment] = field(default_factory=list)

    def _roles_for(self, portfolio_id: uuid.UUID | None, legal_entity_id: uuid.UUID | None = None) -> set[str]:
        """Roles that apply to a scope: organization roles always; portfolio / legal-entity roles only for that exact
        portfolio / entity. With neither given, only organization roles apply (consolidated scope)."""
        roles = set()
        for a in self.assignments:
            if a.scope_type == "organization":
                roles.add(a.role)
            elif portfolio_id is not None and a.scope_type == "portfolio" and a.scope_id == portfolio_id:
                roles.add(a.role)
            elif legal_entity_id is not None and a.scope_type == "legal_entity" and a.scope_id == legal_entity_id:
                roles.add(a.role)
        return roles

    @staticmethod
    def _grants(role: str, permission: str) -> bool:
        perms = PERMISSIONS.get(role, set())
        return "*" in perms or permission in perms

    def can(self, permission: str, portfolio_id: uuid.UUID | None = None,
            legal_entity_id: uuid.UUID | None = None) -> bool:
        return any(self._grants(r, permission) for r in self._roles_for(portfolio_id, legal_entity_id))

    def can_in_any_scope(self, permission: str) -> bool:
        """The permission is held in at least one scope (rows are still limited by RLS)."""
        return any(self._grants(a.role, permission) for a in self.assignments)

    def require_any_scope(self, permission: str) -> None:
        if not self.can_in_any_scope(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission}")

    def require_financial_scope(self, permission: str, *, portfolio_id: uuid.UUID | None = None,
                                legal_entity_id: uuid.UUID | None = None) -> None:
        """Financial reports: a portfolio report needs the permission on that portfolio; an entity report needs it on
        that legal entity (or org-wide); a consolidated report (neither given) needs it org-wide."""
        if portfolio_id is not None:
            self.require(permission, portfolio_id)
        elif not self.can(permission, None, legal_entity_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission} for this scope")

    def is_org_owner(self) -> bool:
        return any(a.role == "owner" and a.scope_type == "organization" for a in self.assignments)

    def require(self, permission: str, portfolio_id: uuid.UUID | None = None,
                legal_entity_id: uuid.UUID | None = None) -> None:
        if not self.can(permission, portfolio_id, legal_entity_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission}")


def system_principal(session, org_id: uuid.UUID) -> "Principal":
    """Principal for the organization's system user (worker jobs). The session must already act as that user."""
    from app.core.db import system_principal_id

    return Principal(user_id=system_principal_id(session, org_id), org_id=org_id, is_service=True,
                     assignments=[Assignment("system_worker", "organization", None)])

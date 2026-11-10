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

    def _roles_for(self, portfolio_id: uuid.UUID | None) -> set[str]:
        roles = set()
        for a in self.assignments:
            if a.scope_type == "organization":
                roles.add(a.role)
            elif portfolio_id is not None and a.scope_type == "portfolio" and a.scope_id == portfolio_id:
                roles.add(a.role)
        return roles

    def can(self, permission: str, portfolio_id: uuid.UUID | None = None) -> bool:
        for role in self._roles_for(portfolio_id):
            perms = PERMISSIONS.get(role, set())
            if "*" in perms or permission in perms:
                return True
        return False

    def is_org_owner(self) -> bool:
        return any(a.role == "owner" and a.scope_type == "organization" for a in self.assignments)

    def require(self, permission: str, portfolio_id: uuid.UUID | None = None) -> None:
        if not self.can(permission, portfolio_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission}")


def system_principal(session, org_id: uuid.UUID) -> "Principal":
    """Principal for the organization's system user (worker jobs). The session must already act as that user."""
    from app.core.db import system_principal_id

    return Principal(user_id=system_principal_id(session, org_id), org_id=org_id, is_service=True,
                     assignments=[Assignment("system_worker", "organization", None)])

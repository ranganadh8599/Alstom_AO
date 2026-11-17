"""Access control for run and approval reads.

Most endpoints are user-facing, so "authenticated" is not enough: a caller may
only see work they own, or work they are responsible for overseeing. Without this
layer a tenant could read another tenant's queries and results by guessing an id
(BOLA / IDOR).

Rules
-----
* the requester always sees their own runs
* platform admins see everything
* an approver-class role may review a colleague's run **within the same tenant**
  (human review is a team responsibility, not cross-organisation access)
* everyone else gets 403 — never 404, so the response does not confirm existence

Per-tenant scoping relies on the caller's ``X-Tenant-Id``. If the deployment is
single-tenant and never sends that header, both sides resolve to ``None`` and
reviewers can see each other's work, which is the intended behaviour there.
"""

from __future__ import annotations

from alstom_ao.config import Settings
from alstom_ao.domain.models import ApprovalRequest, IdentityContext, RunRecord
from alstom_ao.errors import ForbiddenError

REVIEWER_ROLES = {"approver", "compliance-officer", "team-lead"}
GLOBAL_ADMIN_ROLES = {"platform-admin"}


def is_platform_admin(identity: IdentityContext) -> bool:
    """Tenant-independent elevation. Approver roles are *not* global."""

    return identity.user.has_any_role(sorted(GLOBAL_ADMIN_ROLES))


def is_privileged(identity: IdentityContext, settings: Settings) -> bool:
    """Backwards-compatible name: platform admins only.

    Approver-class roles are deliberately tenant-scoped; see ``_same_tenant``.
    """

    return is_platform_admin(identity)


def _same_tenant(owner_tenant: str | None, caller_tenant: str | None) -> bool:
    """Equal tenants, including the single-tenant case where no header is sent."""

    return owner_tenant == caller_tenant


def can_view_run(record: RunRecord, identity: IdentityContext, settings: Settings) -> bool:
    owner = record.identity.user
    if owner.user_id == identity.user.user_id:
        return True
    if is_platform_admin(identity):
        return True
    if _same_tenant(owner.tenant_id, identity.user.tenant_id):
        return identity.user.has_any_role(sorted(REVIEWER_ROLES))
    return False


def require_run_access(record: RunRecord, identity: IdentityContext, settings: Settings) -> None:
    if not can_view_run(record, identity, settings):
        raise ForbiddenError(
            "You are not allowed to view this run.",
            details={"run_id": record.run_id},
        )


def can_view_approval(
    approval: ApprovalRequest,
    identity: IdentityContext,
    settings: Settings,
    *,
    tenant_id: str | None = None,
) -> bool:
    payload = approval.payload or {}
    if payload.get("requested_by") == identity.user.user_id:
        return True
    if is_platform_admin(identity):
        return True
    approval_tenant = payload.get("tenant_id") or tenant_id
    if _same_tenant(approval_tenant, identity.user.tenant_id):
        return identity.user.has_any_role(sorted(REVIEWER_ROLES))
    return False


def require_approval_access(
    approval: ApprovalRequest,
    identity: IdentityContext,
    settings: Settings,
    *,
    tenant_id: str | None = None,
) -> None:
    if not can_view_approval(approval, identity, settings, tenant_id=tenant_id):
        raise ForbiddenError(
            "You are not allowed to view this approval.",
            details={"approval_id": approval.approval_id},
        )

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import audit
from ..config import settings
from ..database import get_db
from ..deps import WorkspaceContext, require
from ..logging_conf import get_logger
from ..models import Tier, Workspace

router = APIRouter(prefix="/billing", tags=["billing"])
logger = get_logger(__name__)

# Plans belong to workspaces; only the workspace owner can change them.


@router.post("/checkout")
def create_checkout_session(ctx: WorkspaceContext = Depends(require("billing:manage"))):
    """Mock Stripe Checkout. In production this calls
    `stripe.checkout.Session.create(client_reference_id=workspace.id, ...)`
    and returns the hosted-checkout URL."""
    session_id = f"cs_mock_{ctx.workspace.id}"
    return {
        "checkout_url": f"http://localhost:8000/billing/mock-pay?session_id={session_id}",
        "session_id": session_id,
        "client_reference_id": ctx.workspace.id,
        "price_usd": settings.pro_price_usd,
    }


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Mock Stripe webhook. In production: verify the Stripe signature header,
    then handle `checkout.session.completed` / `customer.subscription.deleted`."""
    event = await request.json()
    event_type = event.get("type")
    logger.info("stripe_webhook", extra={"event_type": event_type})

    if event_type == "checkout.session.completed":
        ref = event.get("data", {}).get("object", {}).get("client_reference_id")
        workspace = db.get(Workspace, int(ref)) if ref else None
        if workspace:
            workspace.tier = Tier.pro
            audit.record(db, "billing.upgrade", workspace_id=workspace.id,
                         target_type="workspace", target_id=workspace.id,
                         details={"source": "webhook"})
            db.commit()
    return {"received": True}


@router.post("/mock-pay")
def mock_pay(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require("billing:manage")),
):
    """Demo shortcut that simulates a completed checkout and upgrades the workspace."""
    ctx.workspace.tier = Tier.pro
    audit.record(db, "billing.upgrade", workspace_id=ctx.workspace.id,
                 target_type="workspace", target_id=ctx.workspace.id,
                 details={"session_id": session_id}, request=request, **ctx.audit_actor())
    db.commit()
    logger.info("workspace_upgraded",
                extra={"workspace_id": ctx.workspace.id, "session_id": session_id})
    return {"status": "paid", "tier": ctx.workspace.tier.value}


@router.post("/cancel")
def cancel_subscription(
    request: Request,
    db: Session = Depends(get_db),
    ctx: WorkspaceContext = Depends(require("billing:manage")),
):
    ctx.workspace.tier = Tier.free
    audit.record(db, "billing.cancel", workspace_id=ctx.workspace.id,
                 target_type="workspace", target_id=ctx.workspace.id,
                 request=request, **ctx.audit_actor())
    db.commit()
    return {"status": "canceled", "tier": ctx.workspace.tier.value}

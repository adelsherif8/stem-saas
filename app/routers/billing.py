from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..logging_conf import get_logger
from ..models import Tier, User

router = APIRouter(prefix="/billing", tags=["billing"])
logger = get_logger(__name__)


@router.post("/checkout")
def create_checkout_session(user: User = Depends(get_current_user)):
    """Mock Stripe Checkout. In production this calls
    `stripe.checkout.Session.create(client_reference_id=user.id, ...)`
    and returns the hosted-checkout URL."""
    session_id = f"cs_mock_{user.id}"
    return {
        "checkout_url": f"http://localhost:8000/billing/mock-pay?session_id={session_id}",
        "session_id": session_id,
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
        user = db.get(User, int(ref)) if ref else None
        if user:
            user.tier = Tier.pro
            db.commit()
    return {"received": True}


@router.post("/mock-pay")
def mock_pay(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Demo shortcut that simulates a completed checkout and upgrades the user."""
    user.tier = Tier.pro
    db.commit()
    logger.info("user_upgraded", extra={"user_id": user.id, "session_id": session_id})
    return {"status": "paid", "tier": user.tier.value}


@router.post("/cancel")
def cancel_subscription(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    user.tier = Tier.free
    db.commit()
    return {"status": "canceled", "tier": user.tier.value}

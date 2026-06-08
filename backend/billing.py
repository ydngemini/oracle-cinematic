import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.connection import get_pool
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.billing")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "price_REPLACE_ME")

BASE_URL = os.getenv("ORACLE_BASE_URL", "http://localhost:5173")

stripe.api_key = STRIPE_SECRET_KEY

# Startup guard — catch misconfiguration before the first real request hits.
if not STRIPE_SECRET_KEY:
    logger.warning(
        "STRIPE_SECRET_KEY not set — Stripe API calls will fail at runtime. "
        "Set this variable before accepting real traffic."
    )
if not STRIPE_WEBHOOK_SECRET:
    logger.warning(
        "STRIPE_WEBHOOK_SECRET not set — all incoming webhooks will be rejected "
        "with SignatureVerificationError. Set this variable before going live."
    )

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class CheckoutRequest(BaseModel):
    tenant_id: str


class CheckoutResponse(BaseModel):
    session_id: str
    url: str


class PortalRequest(BaseModel):
    tenant_id: str


class SubscriptionStatus(BaseModel):
    active: bool
    status: str
    plan: str
    current_period_end: str | None = None


# ---------------------------------------------------------------------------
# POST /billing/create-checkout-session
# ---------------------------------------------------------------------------
@router.post("/create-checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(
    body: CheckoutRequest,
    ctx: TenantContext = Depends(require_context),
):
    # IDOR guard: callers may only open a checkout for their own tenant.
    # platform_admin is allowed to act on behalf of any tenant.
    if not ctx.is_platform_admin and body.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot create a checkout session for a different tenant.",
        )
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            metadata={"tenant_id": body.tenant_id},
            success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/billing/cancel",
            billing_address_collection="auto",
            allow_promotion_codes=True,
        )
    except stripe.error.InvalidRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.AuthenticationError:
        raise HTTPException(status_code=500, detail="Stripe authentication failed")
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")

    return CheckoutResponse(session_id=session.id, url=session.url)


# ---------------------------------------------------------------------------
# POST /billing/create-portal-session
# ---------------------------------------------------------------------------
@router.post("/create-portal-session")
async def create_portal_session(
    body: PortalRequest,
    ctx: TenantContext = Depends(require_context),
):
    # IDOR guard: callers may only manage their own tenant's portal.
    if not ctx.is_platform_admin and body.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot open a billing portal for a different tenant.",
        )

    pool = get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="DB unavailable")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stripe_customer_id FROM subscriptions WHERE tenant_id = $1 LIMIT 1",
            body.tenant_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail="No subscription found")

    try:
        session = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=f"{BASE_URL}/dashboard",
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")

    return JSONResponse(content={"url": session.url})


# ---------------------------------------------------------------------------
# GET /billing/status/{tenant_id}
# ---------------------------------------------------------------------------
@router.get("/status/{tenant_id}", response_model=SubscriptionStatus)
async def subscription_status(
    tenant_id: str,
    ctx: TenantContext = Depends(require_context),
):
    # IDOR guard: agents may only query their own tenant's subscription status.
    if not ctx.is_platform_admin and tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot query subscription status for a different tenant.",
        )

    pool = get_pool()
    if not pool:
        return SubscriptionStatus(active=False, status="no_db", plan="none")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, plan, current_period_end FROM subscriptions "
            "WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 1",
            tenant_id,
        )

    if not row:
        return SubscriptionStatus(active=False, status="none", plan="none")

    # "past_due" is Stripe's payment-retrying grace period — the subscription
    # is still active and the customer should retain access until it lapses to
    # "canceled" or "unpaid". Including it here prevents a service interruption
    # during the retry window.
    active = row["status"] in ("active", "trialing", "past_due")
    period_end = row["current_period_end"].isoformat() if row["current_period_end"] else None

    return SubscriptionStatus(
        active=active,
        status=row["status"],
        plan=row["plan"],
        current_period_end=period_end,
    )


# ---------------------------------------------------------------------------
# POST /billing/webhook — Stripe webhook handler
# ---------------------------------------------------------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    pool = get_pool()
    if not pool:
        logger.error("[billing] webhook received but DB pool unavailable — returning 503 so Stripe retries")
        # Return 503 (not 200) so Stripe will retry delivery rather than
        # silently dropping the event when the DB is transiently unavailable.
        raise HTTPException(status_code=503, detail="DB unavailable — retry later")

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(pool, obj)

    elif event_type == "invoice.paid":
        await _handle_invoice_paid(pool, obj)

    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(pool, obj)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(pool, obj)

    else:
        logger.debug("[billing] unhandled event: %s", event_type)

    return JSONResponse(content={"received": True})


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------
async def _handle_checkout_completed(pool, session):
    tenant_id = session.get("metadata", {}).get("tenant_id")
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")

    if not tenant_id or not subscription_id:
        logger.warning("[billing] checkout.completed missing tenant_id or subscription")
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id, status)
               VALUES ($1, $2, $3, 'active')
               ON CONFLICT (stripe_subscription_id) DO UPDATE
               SET status = 'active', stripe_customer_id = $2, updated_at = now()""",
            tenant_id, customer_id, subscription_id,
        )

    logger.info("[billing] checkout completed — tenant=%s sub=%s", tenant_id, subscription_id)


async def _handle_invoice_paid(pool, invoice):
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    # Use the top-level invoice `period_end` field — the canonical timestamp for
    # when the paid billing period ends. The previous code drilled into
    # lines.data[0].period.end which is absent for some invoice types (e.g.
    # one-off items, setup fees) and silently sets period_end to NULL.
    period_end = invoice.get("period_end")
    if period_end is None:
        logger.warning("[billing] invoice.paid has no period_end — sub=%s", subscription_id)
    period_end_dt = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE subscriptions SET status = 'active', current_period_end = $2, updated_at = now()
               WHERE stripe_subscription_id = $1""",
            subscription_id, period_end_dt,
        )

    logger.info("[billing] invoice.paid — sub=%s period_end=%s", subscription_id, period_end_dt)


async def _handle_subscription_updated(pool, subscription):
    sub_id = subscription.get("id")
    status = subscription.get("status", "unknown")
    period_end = subscription.get("current_period_end")
    period_end_dt = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE subscriptions SET status = $2, current_period_end = $3, updated_at = now()
               WHERE stripe_subscription_id = $1""",
            sub_id, status, period_end_dt,
        )

    logger.info("[billing] subscription.updated — sub=%s status=%s", sub_id, status)


async def _handle_subscription_deleted(pool, subscription):
    sub_id = subscription.get("id")

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE subscriptions SET status = 'canceled', updated_at = now()
               WHERE stripe_subscription_id = $1""",
            sub_id,
        )

    logger.info("[billing] subscription.deleted — sub=%s", sub_id)

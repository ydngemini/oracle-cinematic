import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from db.connection import get_pool, tenant_tx
from tenancy import Role, TenantContext, require_context

logger = logging.getLogger("oracle.billing")

# Stripe webhooks carry no JWT, so they have no tenant context. Subscriptions
# RLS is `tenant_id = app_current_tenant() OR app_is_platform_admin()` — without
# a GUC context every webhook write/read is silently RLS-filtered (the same trap
# migration 0016 documents for the audit ledger). Run these through a platform-
# admin system context so the policy is satisfied; the explicit tenant_id in each
# query remains the real scoping. (Mirrors disposition_enforcer.SYSTEM_CTX.)
_SYSTEM_CTX = TenantContext(
    agent_id="billing-webhook",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _truthy(v: str) -> bool:
    """Parse a boolean env var: 1/true/yes/on (case-insensitive, ws-tolerant)."""
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "price_REPLACE_ME")

BASE_URL = os.getenv("ORACLE_BASE_URL", "http://localhost:5173")

# Compliance toggles (Jun-2026 legal audit).
# - SaaS sales tax: 25+ jurisdictions tax SaaS; non-collection risks ~30% penalty
#   + multi-year lookback. Stripe Tax auto-computes once enabled in the Stripe
#   Dashboard AND a tax registration exists for the state. Default ON; flip off
#   only if Stripe Tax is not yet activated on the account (else checkout 400s).
STRIPE_AUTOMATIC_TAX = _truthy(os.getenv("STRIPE_AUTOMATIC_TAX", "1"))
# - CA ARL AB 2863 affirmative consent: requires a Terms-of-Service checkbox at
#   checkout. Needs a ToS URL configured in the Stripe Dashboard, so it's gated
#   behind this flag to avoid breaking checkout on accounts without one.
STRIPE_REQUIRE_TOS = _truthy(os.getenv("STRIPE_REQUIRE_TOS", "0"))

# Auto-renew disclosure shown at checkout. CA ARL requires the renewal frequency,
# amount, and cancellation path be clear and conspicuous before purchase.
AUTO_RENEW_DISCLOSURE = os.getenv(
    "ORACLE_AUTORENEW_DISCLOSURE",
    "This is an auto-renewing monthly subscription. You will be charged each month "
    "until you cancel. You can cancel anytime from Billing in your dashboard.",
)

stripe.api_key = STRIPE_SECRET_KEY

# Live-key safety interlock. A sk_live_* key bills REAL cards on every checkout and
# webhook. Allow it only in a production posture; in dev/test (the default ORACLE_ENV)
# refuse to boot rather than risk a test checkout charging a real customer. The escape
# hatch ORACLE_ALLOW_LIVE_STRIPE=1 lets an operator exercise the live key locally on
# purpose. Mirrors config.validate_or_die()'s fail-fast philosophy.
_IS_LIVE_STRIPE = STRIPE_SECRET_KEY.startswith("sk_live_")
if _IS_LIVE_STRIPE and config.IS_DEV and not _truthy(os.getenv("ORACLE_ALLOW_LIVE_STRIPE", "")):
    raise RuntimeError(
        "Refusing to start: a LIVE Stripe key (sk_live_*) is configured while "
        "ORACLE_ENV is dev/unset — live keys charge real cards. Use a sk_test_* key "
        "for development, set ORACLE_ENV=production for a real deployment, or set "
        "ORACLE_ALLOW_LIVE_STRIPE=1 to override this interlock deliberately."
    )
if _IS_LIVE_STRIPE:
    logger.warning("Stripe LIVE mode active (sk_live_*) — real charges WILL be processed.")
elif STRIPE_SECRET_KEY:
    logger.info("Stripe test mode (sk_test_*) — no real charges.")

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
    # CA ARL / FTC Section 5: affirmative consent + a clear auto-renew disclosure
    # before purchase. SaaS sales tax: collect billing address + tax IDs so Stripe
    # Tax can compute and remit. Cancellation is self-serve via the billing portal
    # (channel parity — same online channel used to subscribe).
    session_kwargs = dict(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        metadata={"tenant_id": body.tenant_id},
        success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/billing/cancel",
        # "required" (not "auto") so Stripe Tax always has an address to source from.
        billing_address_collection="required",
        allow_promotion_codes=True,
        tax_id_collection={"enabled": True},
        custom_text={"submit": {"message": AUTO_RENEW_DISCLOSURE}},
    )
    if STRIPE_AUTOMATIC_TAX:
        session_kwargs["automatic_tax"] = {"enabled": True}
    if STRIPE_REQUIRE_TOS:
        session_kwargs["consent_collection"] = {"terms_of_service": "required"}
        session_kwargs["custom_text"]["terms_of_service_acceptance"] = {
            "message": AUTO_RENEW_DISCLOSURE,
        }
    try:
        session = stripe.checkout.Session.create(**session_kwargs)
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

    async with tenant_tx(ctx) as conn:
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

    # Platform admin (the operator) is EXEMPT from billing — full access with no
    # subscription. Keyed on the VERIFIED role (not the tenant_id path param, which
    # the FE resolver may set to a demo/other tenant), so the access gate always
    # opens for the operator. This endpoint only backs the front-end gate; the admin
    # dashboard reads aggregate subscription counts elsewhere (admin_ops.py).
    if ctx.is_platform_admin:
        return SubscriptionStatus(active=True, status="platform_admin", plan="platform")

    pool = get_pool()
    if not pool:
        return SubscriptionStatus(active=False, status="no_db", plan="none")

    async with tenant_tx(ctx) as conn:
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

    async with tenant_tx(_SYSTEM_CTX) as conn:
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

    async with tenant_tx(_SYSTEM_CTX) as conn:
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

    async with tenant_tx(_SYSTEM_CTX) as conn:
        await conn.execute(
            """UPDATE subscriptions SET status = $2, current_period_end = $3, updated_at = now()
               WHERE stripe_subscription_id = $1""",
            sub_id, status, period_end_dt,
        )

    logger.info("[billing] subscription.updated — sub=%s status=%s", sub_id, status)


async def _handle_subscription_deleted(pool, subscription):
    sub_id = subscription.get("id")

    async with tenant_tx(_SYSTEM_CTX) as conn:
        await conn.execute(
            """UPDATE subscriptions SET status = 'canceled', updated_at = now()
               WHERE stripe_subscription_id = $1""",
            sub_id,
        )

    logger.info("[billing] subscription.deleted — sub=%s", sub_id)

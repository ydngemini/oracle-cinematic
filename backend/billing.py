import json
import logging
import os
import re
import recovery_mode
from datetime import datetime, timezone
import asyncio
import time
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config
from db.connection import get_pool, tenant_tx
from tenancy import Role, TenantContext, require_context, require_role

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
# Optional usage-based price added alongside the flat one. Unset = flat-only,
# which is the current shipped plan; usage still accrues locally either way.
# The ledger and the drain live in billing_usage.py.
STRIPE_METERED_PRICE_ID = os.getenv("STRIPE_METERED_PRICE_ID", "").strip()

BASE_URL = config.public_base_url()

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

# The default above is a placeholder so this module imports without billing
# configuration. Reaching checkout with it means Stripe answers "No such price"
# and — because the handler echoed Stripe's text straight back — a paying
# customer read that at the moment of payment. A misconfiguration should never
# be discovered by the buyer, so it is caught before the Stripe call instead.
_PRICE_PLACEHOLDER = "price_REPLACE_ME"


def _price_misconfigured() -> str | None:
    """Why checkout cannot run, or None when the plan price looks usable."""
    price = (STRIPE_PRICE_ID or "").strip()
    if not price or price == _PRICE_PLACEHOLDER:
        return "STRIPE_PRICE_ID is unset (still the placeholder default)"
    if not price.startswith("price_"):
        # A product id (prod_...) here is the common slip, and Stripe's refusal
        # names neither variable nor expectation.
        return "STRIPE_PRICE_ID is not a Stripe price id — expected price_..."
    return None


stripe.api_key = STRIPE_SECRET_KEY

# Live-key safety interlock. A sk_live_* key bills REAL cards on every checkout and
# webhook. Allow it only in a production posture; in dev/test (the default ORACLE_ENV)
# refuse to boot rather than risk a test checkout charging a real customer. The escape
# hatch ORACLE_ALLOW_LIVE_STRIPE=1 lets an operator exercise the live key locally on
# purpose. Mirrors config.validate_or_die()'s fail-fast philosophy.
_IS_LIVE_STRIPE = STRIPE_SECRET_KEY.startswith("sk_live_")
# `not config.IS_PROD`, never `config.IS_DEV`: the guard must catch every
# environment that is not explicitly production, including the unset default.
if _IS_LIVE_STRIPE and not config.IS_PROD and not _truthy(os.getenv("ORACLE_ALLOW_LIVE_STRIPE", "")):
    raise RuntimeError(
        "Refusing to start: a LIVE Stripe key (sk_live_*) is configured while "
        "ORACLE_ENV is not production — live keys charge real cards. Use a sk_test_* key "
        "for development, set ORACLE_ENV=production for a real deployment, or set "
        "ORACLE_ALLOW_LIVE_STRIPE=1 to override this interlock deliberately."
    )
if _IS_LIVE_STRIPE:
    logger.warning("Stripe LIVE mode active (sk_live_*) — real charges WILL be processed.")
elif STRIPE_SECRET_KEY:
    logger.info("Stripe test mode (sk_test_*) — no real charges.")

_ENTITLED_STATUSES = ("active", "trialing", "past_due")


def billing_enforced() -> bool:
    """Server-side entitlement is on everywhere but development unless an
    operator turns it off explicitly (the load-test topology does)."""
    raw = os.getenv("ORACLE_BILLING_ENFORCED")
    if raw is None or not raw.strip():
        return not config.IS_DEV
    return raw.strip().lower() not in ("0", "false", "no", "off")


async def require_active_subscription(
    ctx: TenantContext = Depends(require_context),
) -> TenantContext:
    """Refuse platform spend and customer contact for a tenant that is not paying.

    /billing/status only ever backed the front-end gate, and self-serve signup
    creates a broker_owner with no payment — so an unpaid or cancelled tenant
    could buy phone numbers on the platform carrier account, register 10DLC
    brands, release outbound sends and queue GPU work (review BILL-2). Applied
    to exactly those routes; reading and editing your own CRM is not gated.
    """
    if ctx.is_platform_admin:
        return ctx
    from db import connection as _dbc

    if _dbc.get_pool() is None:
        # No database (unit tests, degraded boot): nothing to read either gate from.
        if not billing_enforced():
            return ctx
        raise HTTPException(status_code=503, detail="Billing status is unavailable.")
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT (SELECT status FROM subscriptions WHERE tenant_id = $1 "
            "        ORDER BY created_at DESC LIMIT 1) AS status, "
            "       (SELECT lifecycle_state FROM tenants WHERE id = $1) AS lifecycle",
            ctx.tenant_id,
        )
    # A brokerage that is closing (or suspended) spends nothing and contacts
    # nobody, paid or not — enforced even where billing enforcement is off.
    if row is not None and row["lifecycle"] not in (None, "active"):
        raise HTTPException(
            status_code=423,
            detail="This brokerage is closing; outbound actions are disabled.",
        )
    if not billing_enforced():
        return ctx
    status_value = row["status"] if row is not None else None
    if status_value not in _ENTITLED_STATUSES:
        raise HTTPException(
            status_code=402,
            detail="An active Neoh subscription is required for this action.",
        )
    return ctx


def _webhook_secret_configured() -> bool:
    """A real Stripe endpoint secret: non-empty and in Stripe's whsec_ format."""
    secret = (STRIPE_WEBHOOK_SECRET or "").strip()
    return secret.startswith("whsec_") and len(secret) >= 16


# Startup guard — catch misconfiguration before the first real request hits.
if not STRIPE_SECRET_KEY:
    logger.warning(
        "STRIPE_SECRET_KEY not set — Stripe API calls will fail at runtime. "
        "Set this variable before accepting real traffic."
    )
if not STRIPE_WEBHOOK_SECRET:
    logger.warning(
        "STRIPE_WEBHOOK_SECRET not set — the webhook answers 503 to every "
        "delivery until it is. Set this variable before going live."
    )
if _price_misconfigured():
    logger.warning(
        "STRIPE_PRICE_ID is unset or a placeholder — checkout will refuse to "
        "start until it is set. Set this variable before accepting real traffic."
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
    # The brokerage's subscription, card and invoices are the owner's to
    # manage. Any agent could open the Stripe portal and cancel the whole
    # brokerage's plan (review BILL-4/TEN-3). Authorization before anything else.
    require_role(ctx, Role.BROKER_OWNER)
    # A restored copy must never reach Stripe: it holds the same live key as
    # production, and the customer records it is reasoning from may be hours
    # stale. 503 rather than the guard's own exception, because this is an HTTP
    # surface and a 500 would read as a bug rather than a deliberate refusal.
    if recovery_mode.is_recovery_mode():
        raise HTTPException(
            status_code=503,
            detail="Billing is unavailable: this instance is running in recovery mode.",
        )
    # IDOR guard: callers may only open a checkout for their own tenant.
    # platform_admin is allowed to act on behalf of any tenant.
    if not ctx.is_platform_admin and body.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot create a checkout session for a different tenant.",
        )
    # Refuse before calling Stripe rather than after. The customer is already at
    # the payment step; an operator-configuration failure must not surface there
    # as though they had done something wrong.
    price_problem = _price_misconfigured()
    if price_problem:
        logger.error("Refusing to open a checkout session: %s", price_problem)
        raise HTTPException(
            status_code=503,
            detail="Checkout is unavailable — the subscription plan is not configured.",
        )
    # CA ARL / FTC Section 5: affirmative consent + a clear auto-renew disclosure
    # before purchase. SaaS sales tax: collect billing address + tax IDs so Stripe
    # Tax can compute and remit. Cancellation is self-serve via the billing portal
    # (channel parity — same online channel used to subscribe).
    # Base flat price, plus an OPTIONAL metered line. A metered price must be
    # sent without `quantity` — Stripe rejects the item otherwise, since the
    # quantity comes from meter events (see billing_usage.drain_usage_to_stripe)
    # rather than from checkout. When STRIPE_METERED_PRICE_ID is unset this is
    # byte-for-byte the previous flat-plan behaviour.
    line_items: list[dict] = [{"price": STRIPE_PRICE_ID, "quantity": 1}]
    if STRIPE_METERED_PRICE_ID:
        line_items.append({"price": STRIPE_METERED_PRICE_ID})

    session_kwargs = dict(
        mode="subscription",
        line_items=line_items,
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
    # One paid plan per brokerage. A second checkout while one is live used to
    # create a second subscription that billed alongside the first.
    from db import connection as _dbc

    live = None
    if _dbc.get_pool() is not None:
        async with tenant_tx(ctx) as conn:
            live = await conn.fetchval(
                "SELECT 1 FROM subscriptions WHERE tenant_id = $1 "
                "AND status IN ('active','trialing','past_due') LIMIT 1", body.tenant_id)
    if live:
        raise HTTPException(status_code=409,
                            detail="This brokerage already has an active plan. Manage it from Billing.")
    # Same key for the same brokerage within ten minutes: a double click (or a
    # retry after a timeout) returns the same session instead of a second one.
    idem = f"checkout:{body.tenant_id}:{STRIPE_PRICE_ID}:{int(time.time() // 600)}"
    try:
        # Off the event loop and bounded: the SDK call is synchronous (80 s x 3
        # attempts by default), and on the loop it froze every request.
        session = await asyncio.wait_for(asyncio.to_thread(
            lambda: stripe.checkout.Session.create(**session_kwargs, idempotency_key=idem)), timeout=45)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Checkout is temporarily unavailable. Please try again shortly.")
    except stripe.error.InvalidRequestError as exc:
        # Stripe's message names prices, tax registration and account state.
        # That is operator detail: the request body here is only a tenant_id, so
        # an InvalidRequestError is never the caller's fault, and returning it as
        # a 400 with Stripe's text told the buyer otherwise.
        logger.error("Stripe rejected the checkout session: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Checkout could not be started. Please try again shortly.",
        )
    except stripe.error.AuthenticationError:
        raise HTTPException(status_code=500, detail="Stripe authentication failed")
    except stripe.error.StripeError as exc:
        # Same reasoning as InvalidRequestError above: Stripe's text is operator
        # detail, and this branch was leaking it too.
        logger.error("Stripe failed to create the checkout session: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Checkout could not be started. Please try again shortly.",
        )

    return CheckoutResponse(session_id=session.id, url=session.url)


# ---------------------------------------------------------------------------
# POST /billing/create-portal-session
# ---------------------------------------------------------------------------
@router.post("/create-portal-session")
async def create_portal_session(
    body: PortalRequest,
    ctx: TenantContext = Depends(require_context),
):
    # The brokerage's subscription, card and invoices are the owner's to
    # manage. Any agent could open the Stripe portal and cancel the whole
    # brokerage's plan (review BILL-4/TEN-3). Authorization before anything else.
    require_role(ctx, Role.BROKER_OWNER)
    # A restored copy must never reach Stripe: it holds the same live key as
    # production, and the customer records it is reasoning from may be hours
    # stale. 503 rather than the guard's own exception, because this is an HTTP
    # surface and a 500 would read as a bug rather than a deliberate refusal.
    if recovery_mode.is_recovery_mode():
        raise HTTPException(
            status_code=503,
            detail="Billing is unavailable: this instance is running in recovery mode.",
        )
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
            "SELECT stripe_customer_id FROM subscriptions WHERE tenant_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            body.tenant_id,
        )

    # A subscription row with no customer id is not "no subscription" — it is a
    # broken one, and passing customer=None to Stripe turned it into a 502
    # carrying Stripe's own wording. Both now read the same to the customer,
    # because from their side both mean the same thing: the portal is not there.
    if not row or not row["stripe_customer_id"]:
        raise HTTPException(status_code=404, detail="No subscription found")

    try:
        session = await asyncio.wait_for(asyncio.to_thread(
            lambda: stripe.billing_portal.Session.create(
                customer=row["stripe_customer_id"],
                return_url=f"{BASE_URL}/dashboard",
            )), timeout=30)
    except stripe.error.StripeError as exc:
        # This is the cancellation path — CA ARL requires cancelling to be as
        # easy as subscribing — so a failure here is one a customer is likely to
        # be angry about already. Handing them Stripe's internal wording on top
        # helps nobody, and it names account state they should not see.
        logger.error("Stripe failed to open the billing portal: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="The billing portal could not be opened. Please try again shortly.",
        )

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
    # An empty signing secret is not "rejects everything": stripe-python
    # verifies an HMAC keyed with "" like any other, so anyone could forge a
    # checkout.session.completed and activate (or cancel) any brokerage's
    # subscription (review BILL-1). Refuse before verifying anything.
    if not _webhook_secret_configured():
        logger.error("[billing] webhook refused: STRIPE_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="Billing webhooks are not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # construct_event is the signature check. The handlers then read the SAME
    # verified bytes as plain JSON: since stripe-python 15, StripeObject is no
    # longer a dict, so every handler's `.get(...)` raised KeyError('get') —
    # each checkout.session.completed returned 500, Stripe retried, and the
    # subscription never activated (found by the Mission 8 webhook load test;
    # unit tests passed plain dicts, so they could not see it).
    verified = json.loads(payload)
    event_type = verified["type"]
    obj = verified["data"]["object"]
    if event_type != event["type"]:  # the parsed and verified views must agree
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    pool = get_pool()
    if not pool:
        logger.error("[billing] webhook received but DB pool unavailable — returning 503 so Stripe retries")
        # Return 503 (not 200) so Stripe will retry delivery rather than
        # silently dropping the event when the DB is transiently unavailable.
        raise HTTPException(status_code=503, detail="DB unavailable — retry later")

    handler = {
        "checkout.session.completed": _handle_checkout_completed,
        "invoice.paid": _handle_invoice_paid,
        "customer.subscription.updated": _handle_subscription_updated,
        "customer.subscription.deleted": _handle_subscription_deleted,
    }.get(event_type)
    if handler is None:
        logger.debug("[billing] unhandled event: %s", event_type)
        return JSONResponse(content={"received": True})

    event_id = str(verified.get("id") or "")
    event_created = int(verified.get("created") or 0)
    # The ledger's CHECK (0118) refuses any other shape. Hitting it inside the
    # transaction answered 500, and Stripe redelivers a 5xx for days. A signed
    # event we can never record is a permanent 400.
    if not _EVENT_ID_SHAPE.fullmatch(event_id):
        logger.warning("[billing] refused %s with malformed event id %r", event_type, event_id[:80])
        raise HTTPException(status_code=400, detail="Invalid webhook event id")
    # The event id and its effect commit together (0118): a failed delivery
    # leaves no row, so Stripe's retry runs; a processed one makes every
    # replay — and every concurrent duplicate — a no-op.
    try:
        async with tenant_tx(_SYSTEM_CTX) as conn:
            first = await conn.fetchval(
                "INSERT INTO stripe_webhook_events (event_id, event_type, event_created) "
                "VALUES ($1, $2, $3) ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                event_id, event_type, event_created,
            )
            if first is None:
                logger.info("[billing] duplicate event ignored: %s %s", event_type, event_id)
                return JSONResponse(content={"received": True, "duplicate": True})
            try:
                await handler(conn, obj, event_created)
            except _SubscriptionNotYetKnown:
                # Stripe does not guarantee order: invoice.paid or
                # subscription.updated can arrive before checkout.session.completed.
                # Recording it as processed lost it for good. Roll back (the event
                # id is not kept) and ask Stripe to redeliver later.
                raise _RetryLater()
    except _RetryLater:
        logger.info("[billing] %s %s arrived before its subscription; asking Stripe to redeliver",
                    event_type, event_id)
        return JSONResponse(status_code=503, content={"received": False, "retry": True},
                            headers={"Retry-After": "60"})

    return JSONResponse(content={"received": True})


_EVENT_ID_SHAPE = re.compile(r"evt_[A-Za-z0-9]{1,250}")


class _SubscriptionNotYetKnown(Exception):
    pass


class _RetryLater(Exception):
    pass


async def _require_known(conn, status_line: str, sub_id: Optional[str]) -> None:
    """After an UPDATE that touched nothing: stale (fine) or not yet known (retry)."""
    if str(status_line).endswith(" 0") and sub_id:
        known = await conn.fetchval("SELECT 1 FROM subscriptions WHERE stripe_subscription_id = $1", sub_id)
        if not known:
            raise _SubscriptionNotYetKnown(sub_id)


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------
async def _handle_checkout_completed(conn, session, event_created: int = 0):
    tenant_id = session.get("metadata", {}).get("tenant_id")
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")

    if not tenant_id or not subscription_id:
        logger.warning("[billing] checkout.completed missing tenant_id or subscription")
        return

    # A completed checkout is not a payment: delayed methods (ACH, bank
    # debit) complete with payment_status 'unpaid' and settle — or fail — later.
    # Only a paid (or free) session grants access; invoice.paid does the rest.
    payment_status = session.get("payment_status")
    status = "active" if payment_status in (None, "paid", "no_payment_required") else "incomplete"

    await conn.execute(
        """INSERT INTO subscriptions (tenant_id, stripe_customer_id, stripe_subscription_id,
                                      status, last_event_created)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (stripe_subscription_id) DO UPDATE
           SET status = EXCLUDED.status, stripe_customer_id = EXCLUDED.stripe_customer_id,
               last_event_created = EXCLUDED.last_event_created, updated_at = now()
           WHERE subscriptions.last_event_created <= EXCLUDED.last_event_created
             AND subscriptions.tenant_id = EXCLUDED.tenant_id""",
        tenant_id, customer_id, subscription_id, status, event_created,
    )

    logger.info("[billing] checkout completed — tenant=%s sub=%s status=%s", tenant_id, subscription_id, status)


def _invoice_subscription_id(invoice) -> Optional[str]:
    """API 2025-03-31.basil moved the field under parent.subscription_details."""
    direct = invoice.get("subscription")
    if direct:
        return direct if isinstance(direct, str) else direct.get("id")
    details = ((invoice.get("parent") or {}).get("subscription_details") or {})
    nested = details.get("subscription")
    return nested if isinstance(nested, str) or nested is None else nested.get("id")


async def _handle_invoice_paid(conn, invoice, event_created: int = 0):
    subscription_id = _invoice_subscription_id(invoice)
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

    # Not if a newer event (a cancellation, say) has already been applied.
    status_line = await conn.execute(
        """UPDATE subscriptions SET status = 'active', current_period_end = $2,
                  last_event_created = $3, updated_at = now()
           WHERE stripe_subscription_id = $1 AND last_event_created <= $3""",
        subscription_id, period_end_dt, event_created,
    )
    await _require_known(conn, status_line, subscription_id)

    logger.info("[billing] invoice.paid — sub=%s period_end=%s", subscription_id, period_end_dt)


async def _handle_subscription_updated(conn, subscription, event_created: int = 0):
    sub_id = subscription.get("id")
    status = subscription.get("status", "unknown")
    period_end = subscription.get("current_period_end")
    if period_end is None:
        # 2025-03-31.basil moved the period onto each subscription item.
        items = ((subscription.get("items") or {}).get("data") or [])
        ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
        period_end = max(ends) if ends else None
    period_end_dt = datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None

    status_line = await conn.execute(
        """UPDATE subscriptions SET status = $2, current_period_end = $3,
                  last_event_created = $4, updated_at = now()
           WHERE stripe_subscription_id = $1 AND last_event_created <= $4""",
        sub_id, status, period_end_dt, event_created,
    )
    await _require_known(conn, status_line, sub_id)

    logger.info("[billing] subscription.updated — sub=%s status=%s", sub_id, status)


async def _handle_subscription_deleted(conn, subscription, event_created: int = 0):
    sub_id = subscription.get("id")

    # Deletion is terminal, so it applies even over a "newer" event; it still
    # records its time so nothing older can resurrect the row afterwards.
    await conn.execute(
        """UPDATE subscriptions SET status = 'canceled', updated_at = now(),
                  last_event_created = GREATEST(last_event_created, $2)
           WHERE stripe_subscription_id = $1""",
        sub_id, event_created,
    )

    logger.info("[billing] subscription.deleted — sub=%s", sub_id)

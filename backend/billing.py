import os
import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration — replace with real values or inject from env
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_REPLACE_ME")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_REPLACE_ME")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "price_REPLACE_ME")  # $299/mo recurring

BASE_URL = os.getenv("ORACLE_BASE_URL", "http://localhost:5173")

stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class CheckoutRequest(BaseModel):
    agent_id: str


class CheckoutResponse(BaseModel):
    session_id: str
    url: str


# ---------------------------------------------------------------------------
# POST /billing/create-checkout-session
# ---------------------------------------------------------------------------
@router.post("/create-checkout-session", response_model=CheckoutResponse)
async def create_checkout_session(body: CheckoutRequest):
    """
    Creates a Stripe Checkout Session for the Oracle Swarm License ($299/mo).
    Returns the session_id and the hosted checkout URL for the frontend to redirect to.
    """
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[
                {
                    "price": STRIPE_PRICE_ID,
                    "quantity": 1,
                }
            ],
            metadata={
                "agent_id": body.agent_id,
            },
            success_url=f"{BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/billing/cancel",
            # Collect billing address for tax purposes
            billing_address_collection="auto",
            # Allow promo codes
            allow_promotion_codes=True,
        )
    except stripe.error.InvalidRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except stripe.error.AuthenticationError as exc:
        raise HTTPException(status_code=500, detail=f"Stripe authentication failed: {exc}")
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}")

    return CheckoutResponse(session_id=session.id, url=session.url)


# ---------------------------------------------------------------------------
# POST /billing/webhook  — Stripe webhook handler stub
# ---------------------------------------------------------------------------
@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Stripe webhook endpoint.
    Handles:
      - invoice.paid              → activate / renew agent license
      - customer.subscription.deleted → deactivate agent license
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        # Invalid payload
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "invoice.paid":
        # TODO: retrieve agent_id from subscription metadata and activate license
        subscription_id = data_object.get("subscription")
        customer_id = data_object.get("customer")
        print(
            f"[billing] invoice.paid — customer={customer_id} subscription={subscription_id}"
        )
        # Example: await activate_agent_license(subscription_id)

    elif event_type == "customer.subscription.deleted":
        subscription_id = data_object.get("id")
        customer_id = data_object.get("customer")
        print(
            f"[billing] subscription.deleted — customer={customer_id} subscription={subscription_id}"
        )
        # Example: await deactivate_agent_license(subscription_id)

    elif event_type == "checkout.session.completed":
        agent_id = data_object.get("metadata", {}).get("agent_id")
        subscription_id = data_object.get("subscription")
        print(
            f"[billing] checkout.session.completed — agent={agent_id} subscription={subscription_id}"
        )
        # Example: await link_agent_to_subscription(agent_id, subscription_id)

    else:
        # Silently ignore unhandled event types
        print(f"[billing] unhandled event type: {event_type}")

    return JSONResponse(content={"received": True})

"""Shared pieces of the killer-demo tooling (seed / reset / preflight).

The demo brokerage is ordinary product data in an ordinary tenant. What makes
it safe to reset is not a naming convention but three facts that must ALL
hold before any tool here writes: the tenant id the operator named, the
`tenants.is_demo` flag (migration 0127), and the fixed demo slug. Production
is refused outright, by ORACLE_ENV and by hostname.

Nothing in this module prints a secret. Credentials for the demo logins are
generated once into a gitignored file under performance/out/.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"

TENANT_SLUG = "northstar-realty-demo"
TENANT_NAME = "Northstar Realty (Demo)"

#: The ONE real recipient the demo may ever reach: the operator's own phone
#: (standing authorization, 2026-10-04). Every other number in the seed is a
#: 555-01xx fictional number that no carrier routes.
DEMO_RECIPIENT = "+13024078981"

OWNER = {"email": "jordan.miles@northstar.example.test", "full_name": "Jordan Miles"}
AGENT = {"email": "avery.chen@northstar.example.test", "full_name": "Avery Chen"}

DEFAULT_CREDENTIALS = REPO / "performance" / "out" / "demo" / "northstar-credentials.json"
DEFAULT_STATE = REPO / "performance" / "out" / "demo" / "northstar-state.json"

#: Hosts a demo tool must never write to.
PRODUCTION_HOSTS = frozenset({"neohrs.com", "www.neohrs.com", "app.neohrs.com", "neoh.app"})
_PROD_ENVS = {"prod", "production"}

DEMO_NOTE = "Demo data (synthetic) — Northstar Realty is a fictional brokerage."

# ── the story's data ────────────────────────────────────────────────────────
# Every person and property below is fictional. Addresses are ordinary street
# names, not scraped listings; prices and specs are invented for the story.

SUBJECT_ADDRESS = "123 Main Street, Wilmington, DE 19801"

CLIENTS: list[dict[str, Any]] = [
    {
        "key": "sarah",
        "full_name": "Sarah Johnson",
        "email": "sarah.johnson@example.test",
        # The operator's own phone, so "Text/Call Sarah" reaches a real phone
        # through the real product path — and only that phone.
        "phone": DEMO_RECIPIENT,
        "client_type": "buyer",
        "stage": "active",
        "lead_score": 82,
        "tags": ["buyer", "pre-approved"],
        "preferences": {
            "budget_max": 525000,
            "target_cities": ["Wilmington"],
            "beds_min": 3,
            "property_types": ["single family"],
            "must_haves": ["updated kitchen", "home office"],
        },
        "contact": {"state_code": "DE", "timezone": "America/New_York",
                    "preferred_channel": "sms", "consent": True},
        "notes": [
            "Pre-approved up to $525,000 (lender letter on file). " + DEMO_NOTE,
            "Works from home three days a week — a room she can use as an office "
            "is a must. Wants an updated kitchen; will not take on a renovation.",
        ],
    },
    {
        "key": "marcus",
        "full_name": "Marcus Lee",
        "email": "marcus.lee@example.test",
        "phone": "+13025550142",
        "client_type": "buyer",
        "stage": "active",
        "lead_score": 64,
        "tags": ["buyer", "first-time"],
        "preferences": {"budget_max": 380000, "target_cities": ["Newark"], "beds_min": 2},
        "contact": {"state_code": "DE", "timezone": "America/New_York"},
        "notes": ["First-time buyer near the university. " + DEMO_NOTE],
    },
    {
        "key": "priya",
        "full_name": "Priya Patel",
        "email": "priya.patel@example.test",
        "phone": "+13025550167",
        "client_type": "buyer",
        "stage": "nurture",
        "lead_score": 58,
        "tags": ["buyer", "relocating"],
        "preferences": {"budget_max": 700000, "target_cities": ["Wilmington"], "beds_min": 4},
        "contact": {"state_code": "DE", "timezone": "America/New_York"},
        "notes": ["Relocating in spring; needs four bedrooms. " + DEMO_NOTE],
    },
    {
        "key": "daniel",
        "full_name": "Daniel Brooks",
        "email": "daniel.brooks@example.test",
        "phone": "+13025550118",
        "client_type": "buyer",
        "stage": "lead",
        "lead_score": 41,
        "tags": ["buyer"],
        "preferences": {"budget_max": 450000, "target_cities": ["Wilmington"], "beds_min": 3},
        "contact": {"state_code": "DE", "timezone": "America/New_York"},
        "notes": ["Met at the October open house. " + DEMO_NOTE],
    },
    {
        "key": "elena",
        "full_name": "Elena Ruiz",
        "email": "elena.ruiz@example.test",
        "phone": "+13025550175",
        "client_type": "seller",
        "stage": "active",
        "lead_score": 70,
        "tags": ["seller"],
        "preferences": {},
        "contact": {"state_code": "DE", "timezone": "America/New_York"},
        "notes": ["Selling 123 Main Street; moving closer to family. " + DEMO_NOTE],
    },
    {
        "key": "whitfield",
        "full_name": "Grace Whitfield",
        "email": "grace.whitfield@example.test",
        "phone": "+13025550133",
        "client_type": "both",
        "stage": "closed",
        "lead_score": 35,
        "tags": ["past-client"],
        "preferences": {},
        "contact": {"state_code": "DE", "timezone": "America/New_York"},
        "notes": ["Closed on 27 Brandywine Lane. " + DEMO_NOTE],
    },
]

LISTINGS: list[dict[str, Any]] = [
    {
        "key": "main",
        "address": SUBJECT_ADDRESS,
        "price": 499000, "beds": 3, "baths": 2, "sqft": 1850, "status": "active",
        "property_type": "single family",
        "features": [
            "Kitchen updated in 2024 (quartz counters, new appliances)",
            "Den off the living room, used by the sellers as a home office",
            "Fenced back yard",
            "Detached one-car garage",
        ],
        "seller": "elena",
    },
    {
        "key": "elm",
        "address": "48 Elm Court, Wilmington, DE 19803",
        "price": 489000, "beds": 3, "baths": 2, "sqft": 1720, "status": "pending",
        "property_type": "single family",
        "features": ["Updated kitchen", "Third bedroom is 9 x 9 ft"],
        "seller": None,
    },
    {
        "key": "brandywine",
        "address": "27 Brandywine Lane, Wilmington, DE 19806",
        "price": 515000, "beds": 3, "baths": 2.5, "sqft": 1980, "status": "sold",
        "property_type": "single family",
        "features": ["Finished basement office", "Original 1990s kitchen"],
        "seller": "whitfield",
    },
    {
        "key": "harbor",
        "address": "910 Harbor View Road, Newark, DE 19711",
        "price": 549000, "beds": 4, "baths": 3, "sqft": 2400, "status": "active",
        "property_type": "single family",
        "features": ["Corner lot", "Two-car garage"],
        "seller": None,
    },
]

#: Sarah's recent interest: two similar homes, shown, with her own words.
SHOWINGS: list[dict[str, Any]] = [
    {"client": "sarah", "listing": "elm", "days_ago": 16, "outcome": "interested",
     "feedback": "Loved the updated kitchen. The third bedroom is too small to work as her office."},
    {"client": "sarah", "listing": "brandywine", "days_ago": 9, "outcome": "passed",
     "feedback": "The basement office was ideal, but the kitchen needs a full renovation."},
]


# ── safety ──────────────────────────────────────────────────────────────────

class DemoSafetyError(RuntimeError):
    """A demo tool refused to touch something that is not the demo tenant."""


def refuse_production(base_url: Optional[str] = None) -> None:
    env = os.getenv("ORACLE_ENV", "").strip().lower()
    if env in _PROD_ENVS:
        raise DemoSafetyError(f"ORACLE_ENV={env!r}: demo tools never run against production")
    if base_url:
        host = (urlparse(base_url).hostname or "").lower()
        if host in PRODUCTION_HOSTS or host.endswith(".neohrs.com"):
            raise DemoSafetyError(f"{host} is a production host; demo tools never touch it")


def assert_demo_tenant(row: Optional[dict], expected_id: Optional[str]) -> None:
    """All three must hold: named id, is_demo, demo slug."""
    if row is None:
        raise DemoSafetyError("demo tenant not found")
    if not expected_id:
        raise DemoSafetyError("an explicit --tenant-id is required")
    if str(row.get("id")) != str(expected_id):
        raise DemoSafetyError(f"tenant {row.get('id')} is not the named tenant {expected_id}")
    if row.get("is_demo") is not True:
        raise DemoSafetyError(f"tenant {row.get('id')} is not marked is_demo — refusing")
    if row.get("slug") != TENANT_SLUG:
        raise DemoSafetyError(f"tenant slug {row.get('slug')!r} is not {TENANT_SLUG!r} — refusing")


# ── credentials (generated, gitignored) ─────────────────────────────────────

def load_or_create_credentials(path: Path = DEFAULT_CREDENTIALS) -> dict[str, str]:
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    creds = {
        OWNER["email"]: secrets.token_urlsafe(18),
        AGENT["email"]: secrets.token_urlsafe(18),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(creds, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return creds


def save_state(state: dict, path: Path = DEFAULT_STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(path)


def load_state(path: Path = DEFAULT_STATE) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


# ── database (admin connection; used only where no product path exists) ─────

def ssl_context() -> Optional[ssl.SSLContext]:
    pem = os.getenv("ORACLE_DB_CA_CERT", "").strip()
    ca_file = os.getenv("ORACLE_DB_CA_FILE", "").strip()
    if pem:
        return ssl.create_default_context(cadata=pem)
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    if os.getenv("ORACLE_DB_SSLMODE", "").lower() in ("require", "verify-full"):
        return ssl.create_default_context()
    return None


async def connect_admin():
    import asyncpg

    return await asyncpg.connect(
        host=os.environ["ORACLE_DB_HOST"],
        port=int(os.getenv("ORACLE_DB_PORT", "5432")),
        user=os.getenv("ORACLE_DB_ADMIN_USER") or os.environ["ORACLE_DB_USER"],
        password=os.getenv("ORACLE_DB_ADMIN_PASSWORD") or os.environ["ORACLE_DB_PASSWORD"],
        database=os.getenv("ORACLE_DB_NAME", "oracle"),
        ssl=ssl_context(),
        timeout=20,
    )


async def find_demo_tenant(conn) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT id::text AS id, slug, name, is_demo FROM tenants WHERE slug = $1", TENANT_SLUG
    )
    return dict(row) if row else None


# ── product API client ──────────────────────────────────────────────────────

@dataclass
class ApiError(RuntimeError):
    status: int
    path: str
    detail: str

    def __str__(self) -> str:  # never includes request bodies (passwords)
        return f"{self.path} → HTTP {self.status}: {self.detail[:300]}"


class Api:
    """The product's own HTTP API, signed in as a demo user (cookie session +
    double-submit CSRF), exactly as the browser uses it."""

    def __init__(self, base_url: str, timeout: float = 60.0):
        import httpx

        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout, follow_redirects=False)
        self.csrf = ""

    def close(self) -> None:
        self.http.close()

    def _refresh_csrf(self) -> None:
        r = self.http.get("/auth/csrf")
        if r.status_code == 200:
            self.csrf = r.json().get("csrf_token", "")

    def request(self, method: str, path: str, *, json_body: Any = None,
                files: Any = None, data: Any = None, ok=(200, 201, 202, 204)) -> Any:
        headers = {}
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            if not self.csrf:
                self._refresh_csrf()
            headers["X-CSRF-Token"] = self.csrf
        r = self.http.request(method, path, json=json_body, files=files, data=data, headers=headers)
        if r.status_code == 403 and "CSRF" in r.text:
            self._refresh_csrf()
            headers["X-CSRF-Token"] = self.csrf
            r = self.http.request(method, path, json=json_body, files=files, data=data, headers=headers)
        if r.status_code not in ok:
            raise ApiError(r.status_code, path, r.text)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path: str, **kw) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> Any:
        return self.request("POST", path, json_body=body, **kw)

    def login(self, email: str, password: str) -> dict:
        self.http.cookies.clear()
        self.csrf = ""
        return self.request("POST", "/auth/login",
                            json_body={"agent_id": email, "passphrase": password})

    def accept_policies(self) -> None:
        status = self.get("/auth/policy-acceptance")
        if status.get("required") or status.get("account_security_required"):
            self.post("/auth/policy-acceptance", {
                "policy_version": status["policy_version"],
                "account_security_version": status["account_security_version"],
            })


def backend_on_path() -> None:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("ORACLE_SKIP_DOTENV", "1")

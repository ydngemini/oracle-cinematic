# Production blockers — operator actions

Everything here needs account access, billing, or a vendor fix. None of it is a
code change; the code side is done and covered by tests. Measured 2026-08-22.

Ordered by what blocks the most. **Item 1 makes the rest academic** — there is no
environment to deploy to until it is fixed.

---

## 1. Azure subscription is Disabled → `neohrs.com` is down

`curl https://neohrs.com/` returns nothing; the host is unreachable.

**The trap: `az account list` lies.** It reports `Enabled` for all three
subscriptions. Only the ARM REST call tells the truth:

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>?api-version=2020-01-01" \
  --query state -o tsv
```

Measured 2026-08-22:

| Subscription | `az account list` | ARM (authoritative) |
|---|---|---|
| `120ea104-5498-44f6-8e86-5654a1f4419b` | Enabled | **Disabled** |
| `07915f95-d597-4451-9df6-018d77a50476` | Enabled | token expired — unverified |
| `d03cc686-d248-4804-8918-ab23a2cebd6c` | Enabled | token expired — unverified |

**Action:** restore billing on the subscription hosting `neohrs.com`.
**Verify:** the ARM call returns `Enabled`, then `curl -I https://neohrs.com/`
returns a status line at all.

---

## 2. Regrid token expired 2026-08-15

The token is a **30-day JWT** (`iat 2026-07-16 → exp 2026-08-15`), so this recurs
roughly monthly. Live call returns `401 {"status":"error","message":"Invalid token"}`.

**Action:** regrid.com → account → API token → paste into the gitignored `.env` as
`REGRID_API_TOKEN`. The source reads it at construction, so **restart the backend**;
rotating the value in place does nothing to a running process.

**Verify:**

```bash
curl -s localhost:8000/api/data/health | jq '.sources.regrid_parcel'
# credential_expired: false, credential_days_remaining: ~30
```

The app now states this itself: `/api/data/health` reports
`credential_expired`, `credential_expires_at` and `credential_days_remaining`, warns
within 7 days of expiry, and a rejected credential returns **503 naming the token to
rotate** rather than 502 "temporarily unavailable".

> **This does NOT fill the "0 of 8 core facts" panel.** Those facts come from
> `public_property_records` (`mls_portal.py:527`), populated by `backend/harvesters/*`.
> **No harvester references Regrid**, and nothing in `oracle-app/src` calls
> `/api/data/*`. The Regrid→facts wiring was never built. Renewing the token restores
> the parcel endpoint and building footprints; filling those 8 fields is separate,
> larger work. An earlier claim that this was a no-code fix for all 51 jurisdictions
> was wrong.

---

## 3. Migrations have never run in production

Ledger is **78/78 locally**; production has never existed to run them against.

**Action, once item 1 is resolved:**

```bash
cd backend && python run_migrations.py --reconcile
```

`--reconcile` probes for the objects each migration claims to create and **refuses a
half-applied schema** rather than replaying it. Do not bypass it — `dev-start.sh` used
to, which is how a partial schema could look like a clean start.

---

## 4. Veo needs a billing-enabled GCP project

The video provider seam is built and tested; Veo reports unavailable until this is done,
which is correct behaviour rather than a failure.

Measured 2026-08-22: **all three billing accounts are closed.**

| Billing account | Open |
|---|---|
| `0126F0-F452FA-2DBE3D` | ✗ |
| `0147F3-E64BB3-CDD0C6` | ✗ |
| `01FDF3-1904E7-19FBB8` | ✗ |

**Action:** reopen a billing account, link it to a project, enable
`aiplatform.googleapis.com`, then set `ORACLE_VIDEO_PROVIDER=veo` and
`ORACLE_VEO_PROJECT=<project>`. ADC is already established
(`gcloud auth application-default login`), and no key material goes in `.env`.

**Verify:** `GET /api/video-studio/config` reports `provider_ready: true`. While it is
false, the panel says why and `POST /jobs` 503s **before** quota is spent.

**Deadline:** Azure `sora-2` v2025-12-08 retires **2026-09-15**; OpenAI's Videos API
shuts down 2026-09-24 with no recommended replacement. After that, `ORACLE_VIDEO_PROVIDER=sora`
stops working. The seam means the switch is a config change, not a rewrite.

---

## 5. No GPU path for 3D reconstruction

| Route | State |
|---|---|
| RunPod serverless | Dead — workers never left `initializing`; endpoint deleted 2026-08-14; balance −$0.05 |
| RunPod **pods** (non-serverless) | **Never tried.** Cleanest paid route; ~$10 top-up, 4090 ≈ $0.35–0.69/hr |
| OnCompute paid | Blocked — dashboard wallet is a Privy/Alchemy smart account whose ERC-1271 signatures fail the node's `ecrecover`. 40 COMPY sits in escrow, unusable |
| Local hardware | Intel Iris 540 — integrated, no CUDA. Not viable |
| Colab | Works and is sanctioned (`google-colab-cli` is official Google), verified Tesla T4 · 14.56 GB. **One-off only** — cannot back `reconstruction_providers.py` as a service |

**Walkable tours are not blocked on this.** Tier 2 needs only two 360° photos and no
GPU; the capture mode for it now exists in Property View.

---

## 6. Stripe — untouched by request, one thing recorded

Not changed, per instruction. Recording one finding so it is not rediscovered:

`config.py:31` defines `_DEV_VALUES = {"dev", "development", "local"}` — **`"test"` is
absent**, while `commands_api.py`, `telephony_api.py`, `twilio_call_handler.py`,
`rate_limit_middleware.py` and `listings_feed.py` each re-derive dev-ness *including*
`"test"`. So `ORACLE_ENV=test` reads as **production** to `config.IS_DEV`, which silently
disables the live-key interlock at `billing.py:67-79`.

Also note `.env` currently sets `ORACLE_ALLOW_LIVE_STRIPE=1`, which is the documented
escape hatch and was deliberately enabled.

---

## Not a blocker, but live

`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set. SMS and call controls reach real
phone numbers, and the database holds 100 real Delaware leads. Email
(`SENDGRID_API_KEY`, `SMTP_HOST`) and ElevenLabs are unset and fail closed.

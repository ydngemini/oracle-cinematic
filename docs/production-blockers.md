# Production blockers — operator actions

Everything here needs account access, billing, or a vendor fix. None of it is a
code change; the code side is done and covered by tests.

**Re-measured 2026-09-02.** The deploy target moved from Azure to AWS account
`151105438863` since this doc was written, so the ordering below changed
completely. Four items from that pass are resolved; see "Resolved since
2026-08-22" at the end.

---

## 1. Terraform cannot plan — the configured domain is not registered

`terraform plan` in `infra/terraform` ends with:

```
Error: no matching Route 53 Hosted Zone found
  with data.aws_route53_zone.main, on dns.tf line 18
```

`terraform.tfvars:23` sets `domain_name = "neohrealestate.com"` and `dns.tf` looks
that zone up as a **data source** — it expects the zone to already exist. Measured
2026-09-02 in account `151105438863`:

| | |
|---|---|
| Hosted zones that exist | `neohrs.com.` only |
| `route53domains list-domains` | returns nothing — **neohrealestate.com is not registered** |

**This blocks everything else.** Nothing can be applied until it resolves, and it
is a branding decision, not a technical one:

- **Register `neohrealestate.com`** — `route53domains` creates the hosted zone and
  points the registrar at it in one step, which is why `terraform.tfvars` chose it
  over reusing `neohrs.com`. Costs a registration fee and needs billing.
- **Or point `domain_name` at `neohrs.com`**, whose zone already exists
  (`Z01948911TPWLLT18DY2W`). One-line change, no purchase, but the product ships
  on the old name.

---

## 2. The application layer has never been applied — 64 resources

The foundation is up; the thing that serves traffic is not.

| Component | State (measured 2026-09-02) |
|---|---|
| Aurora `neoh-prod-aurora` 16.14 | **available** |
| ECR repositories (backend, frontend, observability, reconstruction) | exist |
| ECS cluster `neoh-prod` | exists |
| **ECS services** | **none — nothing is running** |
| **Application Load Balancer** | **none** |
| Route53 zone | `neohrs.com` only (see item 1) |

`terraform plan` reports **64 to add, 0 to change, 0 to destroy** — ALB, WAF and
its association, the ECS services, DNS records and the ACM certificate.

**Action:** resolve item 1, then `terraform apply`.

---

## 3. Container images predate the current code

Both `neoh/backend:latest` and `neoh/frontend:latest` were pushed
**2026-08-28T05:17**. Everything since is absent from them, including the AI tool
execution ledger (migrations 0087-0088), the geocoder cascade and canary, the
index migrations 0085-0086 and 0089-0090, and the pypdf CVE bump.

The frontend image also carries a build-time API base URL. It was baked against
the by-then-dead `neoh.app`, so it must be rebuilt against whichever domain item 1
settles on — a rebuild is required regardless of that choice.

**Action:** rebuild and push both after item 1, before `terraform apply`.

---

## 4. Migrations have never run in production

Ledger is **89/89 locally**, 21 applied + 68 reconciled, no drift. Production has
never existed to run them against.

**Action, once the stack is up:**

```bash
cd backend && python run_migrations.py --prebuild-indexes   # concurrent, no lock
cd backend && python run_migrations.py --reconcile
```

`--prebuild-indexes` builds the heavy indexes (0086, 0089-0090) without the
write-blocking ShareLock; on a populated table the in-transaction build would
otherwise exceed the pool's 30s `command_timeout` and fail live writes.
`--reconcile` probes for the objects each migration claims and **refuses a
half-applied schema** rather than replaying it.

## 5. Regrid token expired 2026-08-15

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

## 6. Veo needs a billing-enabled GCP project

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

## 7. No GPU path for 3D reconstruction

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

## 8. Stripe — the recorded finding is now FIXED

Not changed, per instruction. Recording one finding so it is not rediscovered:

**Fixed 2026-09-02, and it was wider than recorded here.** The guard read
`config.IS_DEV`, so it fired only for `dev|development|local`. Measured:

    ORACLE_ENV unset      -> sk_live_* key SILENTLY ACCEPTED
    ORACLE_ENV=test       -> sk_live_* key SILENTLY ACCEPTED
    ORACLE_ENV=staging    -> sk_live_* key SILENTLY ACCEPTED

with **unset being the default**, and the guard's own message claiming it covered
"dev/unset". The root cause was using `not IS_DEV` to mean "is production" — two
different questions with every unrecognised value sitting in the gap. `config.py`
now defines `IS_PROD` separately and `billing.py` gates on `not config.IS_PROD`,
so anything that is not explicitly production refuses a live key. Two tests pin
it, one of them against the source because the failure is silent by construction.

Also note `.env` currently sets `ORACLE_ALLOW_LIVE_STRIPE=1`, which is the documented
escape hatch and was deliberately enabled.

---

## Not a blocker, but live

`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set. SMS and call controls reach real
phone numbers, and the database holds 100 real Delaware leads. Email
(`SENDGRID_API_KEY`, `SMTP_HOST`) and ElevenLabs are unset and fail closed.


---

## Resolved since 2026-08-22

- **Azure subscription Disabled → `neohrs.com` down.** No longer the deploy path;
  the target is AWS `151105438863`. The Azure item is moot rather than fixed.
- **RunPod pods "never tried".** They were tried 2026-08-25 and they work; first
  live GPU reconstructions completed. Serverless remains dead.
- **84 compliance tests not in CI.** CI runs `pytest tests compliance_engine/tests`;
  1,619 pass.
- **pypdf CVEs.** `pip_audit --strict` was failing CI on 3 CVEs in pypdf 6.15.0;
  bumped to 6.16.1, audit clean.

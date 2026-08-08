# Data Access Tiers — what licence each data path actually assumes

**Status:** living register. **Owner:** whoever last changed a data path.
**Last reviewed:** 2026-08-07.

## Why this file exists

The named failure mode for proptech companies is not a bad product — it is
shipping a feature under a data licence that never covered it. A platform starts
with IDX-level access to *display* listings, later adds analytics or backend
enrichment on the same feed, and ships without renegotiating. MLSs fine
violators and can terminate the feed outright, and there are 500+ of them, each
with its own rules. CoStar v. Zillow (copyright, photo reuse) and the FTC's
Sept-2025 action against Zillow are the current reminders that this is enforced,
not theoretical. Sources are in the vault research note
`Research/Deep/2026-08-06 — proptech-revenue-patterns.md`.

Oracle's exposure is low **because of choices already made** — scraping is gated
off, harvesting is gated off, AI media carries disclosure and a manifest. What
was missing was any written record of which tier each path assumes, so nobody
could tell when a new feature quietly outgrew one. That is what this table is.

## The tiers

| Tier | What it permits | What it does NOT permit |
|---|---|---|
| **Public record** | Government-published data: parcels, assessments, deeds, FEMA/EPA/Census. Redistribution generally allowed, attribution often required. | Implying the data is verified or current beyond its stated refresh date. |
| **Licensed API** | Whatever the vendor contract says. Usually query-time use for the licensee's own users. | Bulk re-export, resale, or building a derived database, unless explicitly granted. |
| **IDX** | *Display* of listings to consumers, with rules on attribution, refresh, and which fields may be shown. | Analytics, valuation models, or enrichment built on the feed. Retention past the feed's terms. |
| **VOW** | Display to *registered, authenticated* consumers with a broker relationship. Broader fields than IDX. | Public/anonymous display. Use without the registration flow. |
| **BBO** (broker back-office) | The broker's own transactional use of their own listings. | Anything consumer-facing or cross-brokerage. |
| **Scraped** | Nothing. Zillow's and Redfin's terms both prohibit automated extraction and commercial reuse. | All of it. Anything here must stay behind a default-off gate. |

## Register

**Rule: a row here is a claim about what the code is allowed to do. Change the
code's use of a source → update the row in the same commit.**

| Path / module | Source | Tier | Gate | Notes |
|---|---|---|---|---|
| `data_integrations/census.py`, `census_geocoder.py` | api.census.gov, geocoding.geo.census.gov | Public record | none | Federal, no key for the geocoder. Free-tier rate limits apply. |
| `data_integrations/fema_flood.py`, `openfema.py` | hazards.fema.gov, fema.gov/api/open | Public record | none | Flood zone is advisory; the UI must not present it as a determination. |
| `data_integrations/epa_envirofacts.py` | data.epa.gov | Public record | none | |
| `data_integrations/fbi_crime.py` | api.usa.gov/crime | Public record | API key | Free key. Aggregate only — never per-address crime claims. |
| `data_integrations/eviction_lab.py` | eviction-lab S3 | Public record (academic) | none | Carries an attribution requirement — check the module's licence note before surfacing. |
| `data_integrations/courtlistener.py` | courtlistener.com API | Public record | none | Free tier; court records. |
| `data_integrations/school_districts.py`, `state_gis/` | ArcGIS / state portals | Public record | none | Per-state terms vary; several require attribution. |
| `data_integrations/regrid.py` | app.regrid.com | **Licensed API** | API key | Parcel data under contract. **Query-time use only** — do not build a derived parcel database from it without checking the agreement. |
| `data_integrations/usps.py` | shippingapis.com | Licensed API | API key | USPS terms restrict address validation to shipping-related use. Review before using it for marketing list hygiene. |
| `avm_client.py` → RentCast | api.rentcast.io | **Licensed API** | API key | Valuations. Redistribution of AVM values to third parties is typically NOT granted — keep them inside the tenant. |
| `avm_client.py` → ATTOM | api.gateway.attomdata.com | **Licensed API** | API key (pending) | Same caution as RentCast. Not yet live. |
| `data_integrations/geocoder.py` → Nominatim | nominatim.openstreetmap.org | Public (OSM/ODbL) | none | **Usage policy caps ~1 req/s and requires a real User-Agent.** ODbL share-alike applies to derived geodata. Bulk use is a policy violation even though the data is open. |
| `data_integrations/listings_feed.py` | RESO feed | **IDX (assumed)** | unconfigured | ⚠️ **Highest-risk row.** No feed is configured today, so nothing is being violated. Before one is: write down which tier the agreement grants, and check every consumer of listings data against it. Analytics or AVM enrichment over an IDX feed is the classic breach. |
| `harvesters/`, `county_assessor/` | County sites | Public record | `COUNTY_HARVEST_ENABLED` (**off**) | Gated after a runaway loop against a dead recorder domain. Public records, but scraped access patterns still need rate limiting and a real User-Agent. |
| `spatial_agent.py` photo capture | Zillow / Redfin | **Scraped — prohibited** | `SPATIAL_ALLOW_WEB_SCRAPE` (**off**) | Both sites' terms forbid it. This gate must stay off. Do not add a "just for this tenant" bypass. |

## Before you ship a data feature, answer these

1. **Which row above does this read from?** If none, add the row first.
2. **Is the use the tier permits the use you are making?** Display ≠ analytics.
   Query-time lookup ≠ building a derived dataset. Internal ≠ consumer-facing.
3. **Does anything leave the tenant?** Redistribution is where most licences
   draw the line, and it is the step that turns an internal tool into a breach.
4. **Does the UI overclaim?** A public-record value shown without its
   `record_refreshed_at` and source reads as a verified fact. `enforce_public_property_data`
   exists for this; use it.
5. **If the answer is "I'm not sure"** — that is the answer that ends careers in
   this industry. Ask before shipping, not after the feed is cut.

## Related

- `backend/platform_policy.py` — `enforce_public_property_data`, feature gates.
- `SYPHER_VAULT/10_Active_Builds/Neoh_Free_Data_Sources_Catalog.md` — keyless /
  free-key / do-not-wire breakdown per source.
- `SYPHER_VAULT/Research/Deep/2026-08-06 — proptech-revenue-patterns.md` — the
  enforcement evidence behind this file.

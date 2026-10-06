# Killer demo — presenter script

Five minutes, one property, one buyer, one approved action, all on **staging** through the real product paths. Nothing is staged in the browser. The demo tenant is seeded and reset through the product (`scripts/seed-demo-tenant.py`, `scripts/reset-demo-tenant.py`), and every receipt on screen is the product's own record of what happened.

The automated twin of this script is `scripts/test-killer-demo-playwright.py`. Rehearse with it, and if a beat changes, change both.

## Before the room (15 minutes ahead)

1. **Reset:**

   ```
   python scripts/reset-demo-tenant.py --tenant-id <demo tenant> --base-url <staging> --execute
   ```

   This clears the previous run's texts, calls, approvals and chat, and keeps the stored Space.
2. **Preflight:**

   ```
   python scripts/demo-preflight.py --base-url <staging> --tenant-id <demo tenant> --expected-sha <sha>
   ```

   - **READY** or **READY WITH LIMITATIONS:** go.
   - **BLOCKED:** don't present. Read its blocking line.
   - Say each limitation it prints out loud at the matching beat. The table below lists them.
3. **Calling hours:** a real call is possible only 8 am–8 pm in the recipient's time zone (ET). Outside that window, skip beat 7.
4. **Browser:** a clean browser profile. Sign in as Jordan Miles, the brokerage owner. Credentials are in `performance/out/demo/northstar-credentials.json`, which is gitignored and mode 0600.

## The beats

| # | Do | Say (the point) | What proves it |
|---|---|---|---|
| 1 | Sign in. Dismiss the walkthrough with Esc | "This is a brokerage's morning. Neoh already did the reading." | The Home card **"123 Main Street, Wilmington — May fit Sarah Johnson"** |
| 2 | Click **123 Main Street, Wilmington** | "A $499,000 listing, and the buyers who fit it, from this brokerage's own clients." | **Buyers who may fit** lists Sarah Johnson |
| 3 | Open the **demo 3D space** | "This is a Neoh Space. **This one is a demo room, not this home.** A real capture comes from the agent's walkthrough." | The viewer is labelled *not this home*. Never call it the property |
| 4 | Ask Neoh: *"Who should I call about this?"* | "It answers with evidence, not a guess." | Names Sarah and cites her budget, area or bedrooms |
| 5 | Open Sarah. Ask Neoh to text her about the listing | "Neoh drafts. It never sends on its own." | Receipt: **Text waiting for your approval** |
| 6 | **Review**, then approve the text | "A person approves every outbound action." | Approvals queue → approved |
| 7 | **Neoh** tab: read the text's receipt | "And it tells you what actually happened." | On staging: **Text not sent**, with the product's reason (no registered texting number). That honest refusal is the expected result: say so |
| 8 *(optional, live)* | Ask Neoh to **call** Sarah. Approve it | "This is a real call, to my own phone." | The phone rings. On answer it speaks the AI disclosure. Receipt: **Call placed** |
| 9 | Sarah → **Timeline** | "Everything Neoh did, or refused to do, lands on the person." | The text attempt, plus the call if placed |

## Limitations to state (from preflight-1, 2026-10-04)

| Preflight line | When | What to say |
|---|---|---|
| texting (SMS) | beat 7 | "Texting needs a registered business number. Until then Neoh refuses before contacting any carrier, and says why." |
| 3D is a real capture | beat 3 | "Demo room, labelled as such." |
| realtime AI voice on calls | beat 8 | "When answered, it discloses that it's an AI and says the agent will follow up. There's no live AI conversation on this environment yet." |
| calendar | only if asked to schedule | "Scheduling stages an approval that can't run until a calendar is connected, and Neoh says so." |

## The real call (beat 8): rules

- **Who can be called:** only the allowlisted operator phone. `ORACLE_DEMO_RECIPIENT_ALLOWLIST` is a staging-only secret, and production refuses to boot with it set. Any other number is refused before the carrier.
- **Budget:** at most 3 real texts and 3 real calls in total across all rehearsals and the live demo. **As of 2026-10-06 all 3 calls are used** (0 texts). Ask the operator before placing another.
  - Call 1 (10-04) and call 2 (10-06): Plivo's create response had no request id (only `api_id` + `message`), so the answer webhook refused the call as "unmanaged". **Fixed (6cab880):** Neoh's own reference travels in the callback URLs, and the call state exists before the call is placed.
  - Call 3 (10-06): matched, and the AI disclosure played (answered, 14 s). The receipt still read "needs review", because the acknowledgement insert's `ON CONFLICT` couldn't infer the partial unique index. **Fixed (21ae08e)**, and proven by replaying the write on staging in a rolled-back transaction. Not yet proven by a real call.
  - Rehearse beats 1–7 and 9 with the automated test, without `--real-call`.
- **Hours:** 8 am–8 pm recipient-local only.
- **If the receipt reads "Call needs review":** the carrier outcome is unconfirmed. Don't retry. Check Plivo's call log first. A retry could ring twice.

## If something goes wrong on stage

| Symptom | Line | After |
|---|---|---|
| Neoh is slow (> 20 s) | "It's checking her record and the listing." Wait | Note `neoh_response_s` |
| "Neoh is having trouble answering right now." | "That's the honest failure: nothing was sent." Skip to beat 9 | [ai-provider-down](runbooks/ai-provider-down.md) |
| The Space fails to load | "3D is progressive; the property works without it." Skip to beat 4 | [gpu/space](runbooks/gpu-provider-down.md) |
| An approval is left pending from earlier | Reset, then preflight again (it blocks on stale actions) | `reset-demo-tenant.py` |

## Rehearsals

Each rehearsal runs:

```
python scripts/test-killer-demo-playwright.py --base-url <staging> --tenant-id <demo tenant> --reset --label rehearsal-N
```

Screenshots and timings go to `performance/out/demo/e2e/<label>/`. Record each run in [launch-state](launch-state.md).

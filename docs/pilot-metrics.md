# Pilot metrics

Seven numbers, per brokerage, per week. If a number would not change a
decision in the T+7 review ([`first-brokerage-launch.md`](first-brokerage-launch.md)),
it does not belong here. The operator reads them from operator console →
Pilot metrics (`GET /api/admin/pilot-metrics`, which is being built
alongside this page). The definitions below are the contract that endpoint
implements. Every number is a count or a sum. None includes message content.

| # | Metric | Definition (for the week, for the brokerage) | Why it matters |
|---|---|---|---|
| 1 | **Weekly active agents** | Distinct agents who signed in **and** did at least one thing (opened a record, sent, asked Neoh). Shown as *n of invited* | Adoption. Sign-ins alone flatter |
| 2 | **Neoh conversations** | Neoh threads with at least one agent turn, typed or spoken | Whether agents actually talk to Neoh |
| 3 | **Neoh completed actions** | Actions Neoh proposed **and** that completed: approved commands or ledgered tool writes with a durable result. Excludes drafts discarded and actions that failed | Neoh doing work, not just talking |
| 4 | **Calls and messages initiated through Neoh** | Outbound calls placed and texts/emails sent that originated from a Neoh action or suggestion (not ones the agent started by hand) | The core promise: Neoh moves relationships |
| 5 | **Property/buyer matches acted on** | Matches Neoh surfaced where the agent then contacted the buyer or scheduled a showing within 14 days | Intelligence that turned into action |
| 6 | **Time saved, where measurable** | Only for actions with a measured manual baseline (for example, a drafted follow-up vs typing it). Shown as a range, and as **"not measured"** where no baseline exists. Never extrapolated | Honest value. Most actions have no baseline yet |
| 7 | **Revenue influenced by Neoh** | See below | The outcome that matters most, stated honestly |

## Revenue influenced by Neoh: the honesty rule

The metric uses outcome attribution (`backend/outcome_memory.py`). When a deal
closes, an offer is made, a showing is held or a reply arrives, the
attribution sweep links it to **the last** accepted Neoh decision or approved
command on the same subject (client or property) **inside a per-kind window**:

- reply: 14 days
- showing: 21 days
- offer: 45 days
- closing: 90 days

That is **last-touch association**. It is deliberately simple, because there
is no data yet to fit anything smarter.

So the words are fixed:

- Say **"influenced"**, **"assisted"** or **"associated with"**. Never "caused",
  "generated", "drove" or "earned".
- Revenue influenced is the sum of closed-deal value (or commission, where
  known) for closings **associated** with a Neoh action inside the window.
  Show it next to **total closings in the same period**, plus the count of
  closings associated with **nothing Neoh did**. The attribution sweep writes
  that "found nothing" row on purpose. Without the denominator the number means nothing.
- With fewer than about 10 closings, show counts and the interval
  ("3 of 7 closings followed a Neoh action"), never a percentage on its own.
  Small samples need intervals.
- In the T+7 review, show which Neoh action each association rests on. If
  the agent disputes one, leave it out of the number you report. The number is
  evidence, not a claim.

## What is deliberately not measured

Page views, session length, message volume for its own sake, "AI usage",
model tokens, and anything ranking agents against each other. If one of these
is ever needed, it goes in an ad-hoc query, not on this page.

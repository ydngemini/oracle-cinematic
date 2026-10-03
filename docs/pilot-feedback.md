# Pilot feedback log

One place for what pilot brokerages tell us. Log every item and triage weekly. **A request is not a roadmap item until it has been weighed against the pilot goals.**

## Categories

| Category | Means | Response target |
|---|---|---|
| **BLOCKING** | They cannot do their job in Neoh (lost work, can't call, can't sign in) | Same day. Usually an incident: see [runbooks](runbooks/README.md) |
| **CONFUSING** | They could do it but didn't understand how, or what happened | Triage weekly. Fix copy/flow if two or more brokerages hit it |
| **SLOW** | It worked but felt slow (give the surface and the time) | Measure before changing anything |
| **MISSING** | Something they expected Neoh to do already | Triage weekly. Check whether it exists but is hidden (then it's CONFUSING) |
| **DELIGHTFUL** | Something they loved: keep it, protect it in tests | Share with the team |
| **REQUEST** | A new capability | Batch monthly. Decide against the pilot goals, not the order received |

## Entry format

Copy one block per item:

```
### YYYY-MM-DD · {Brokerage} · {CATEGORY}
- Who: {role — owner / agent}           Surface: {Home / Work / Neoh / person / property / deal / setup / calling / messaging}
- What they said: "{their words}"
- What happened: {facts — release, time, what Neoh did}
- Severity / reach: {1 brokerage | n brokerages}   Repeat of: {link to earlier entry, if any}
- Decision: {fix now | fix this cycle | needs evidence | not doing — why}   Owner: {name}
- Closed: {date + release, or "won't do" + reason}
```

## Weekly triage (30 minutes)

1. Every **BLOCKING** item has an incident or a fix in flight.
2. Merge duplicates. Count how many brokerages each item affects; reach matters more than volume.
3. **CONFUSING** reported by two or more brokerages goes on the polish list.
4. **REQUEST**: note it and leave it. Promote it only if it serves a pilot metric ([sacred metrics](pilot-metrics.md)) or several brokerages need it.
5. Tell each brokerage what happened to its items. Silence reads as "ignored".

## Log

_(newest first)_

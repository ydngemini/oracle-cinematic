# Status and maintenance communication

Templates for telling brokerages what is happening. Send them from the support address to each affected brokerage owner (and to agents, if the owner asks). Keep them short and factual. Never name an internal provider. Never promise a time you don't control.

**Components, as customers know them:**

| Say | Covers (internal) |
|---|---|
| **Neoh** | AI conversations and actions (`ai`) |
| **Calling** | outbound and inbound calls, AI calls (`valkey`, voice provider, `outbound_side_effects`) |
| **Messaging** | texts and email (messaging provider, `email_outbox`) |
| **Property/MLS data** | listings, search freshness (`mls`) |
| **Application** | sign-in, CRM, everything else (`database`, `workers`, the app itself) |

## Incident updates

Send the first update within **30 minutes** of a confirmed customer-visible problem. Update at least every **60 minutes** until resolved, and at every state change.

### Investigating
> **Subject:** [Neoh] Investigating: {Component} issue
>
> We're investigating a problem affecting **{Component}** that started around {time, timezone}.
> {What customers notice — one sentence, e.g. "Some calls may fail to connect."}
> Your data is safe, and {what still works — e.g. "the CRM and Neoh are working normally"}.
> Next update by {time}.

### Identified
> **Subject:** [Neoh] Identified: {Component} issue
>
> We've found the cause of the **{Component}** problem and are working on a fix.
> {Workaround, if any — e.g. "You can still call from your own phone; Neoh will log it when you add a note."}
> Next update by {time}.

### Monitoring
> **Subject:** [Neoh] Monitoring: {Component} recovering
>
> A fix is in place and **{Component}** is working again. We're watching to make sure it stays stable.
> {Anything they need to do — e.g. "Calls that failed between 2:10 and 2:40 PM were not placed; please retry them."}

### Resolved
> **Subject:** [Neoh] Resolved: {Component} issue
>
> The **{Component}** problem is resolved as of {time}. It lasted from {start} to {end}.
> {Impact in one sentence — what did and did not happen. E.g. "No messages were sent twice; 14 texts were delayed by up to 20 minutes."}
> {If relevant: "Nothing was lost." / "Two calls need to be re-placed; we've emailed the agents involved."}
> Sorry for the disruption. Reply to this email with any questions.

Rules:
- State **what did and did not happen**. Neoh's reconciliation tells you which calls and messages were confirmed, which were refused, and which still need review. Use it, and never guess.
- A call or text left "needs review" is neither "sent" nor "failed" until it has been checked. Say so.
- Write a short internal postmortem within 3 business days for any incident longer than 30 minutes. Put it in the pilot log ([pilot feedback](pilot-feedback.md)) with:
  - **timeline**: detected, alerted, mitigated and resolved, with times from `ops_alerts`;
  - **customer impact**: which brokerages, what they saw, and anything lost or delayed;
  - **root cause**;
  - **what alerted us**, or why nothing did;
  - **fix and follow-ups**, each with an owner.
  
  Security incidents follow [security incident response](security-incident-response.md).

## Maintenance notices

Neoh deploys roll API replicas one at a time. Most releases are not noticeable. **A deploy can end a live call** carried by the replica being replaced, and a database maintenance window can make the whole application briefly unavailable. Don't promise zero downtime.

### Planned maintenance (ours)
> **Subject:** [Neoh] Scheduled maintenance: {date}, {start}–{end} {timezone}
>
> We'll be updating Neoh during this window. {Expected effect, e.g. "You may be signed out once, and the app may be unavailable for up to {N} minutes."}
> Please avoid starting calls during the window; a call in progress may end.
> Your data is not affected.

### Provider maintenance (calling, messaging, email)
> **Subject:** [Neoh] Heads-up: {Component} maintenance on {date}
>
> A service Neoh relies on for **{Component}** has scheduled maintenance on {date}, {window}.
> During that time {effect — e.g. "texts may be delayed; they will send automatically afterwards"}.
> Everything else in Neoh works normally.

### MLS feed maintenance
> **Subject:** [Neoh] Listing data updates paused: {date}
>
> {Board name}'s listing feed is down for maintenance on {date}, {window}.
> Listings in Neoh stay available but won't update during that time; they're marked as possibly out of date.
> Updates resume automatically when the feed returns.

### Billing maintenance
> **Subject:** [Neoh] Billing changes paused: {date}
>
> On {date}, {window}, you won't be able to change your subscription or payment method.
> Your access to Neoh is not affected.

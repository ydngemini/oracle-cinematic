# Privacy — questions for counsel

Engineering has built the lifecycle (`docs/data-retention-policy.md`,
`docs/privacy-request-runbook.md`, `docs/account-offboarding-runbook.md`).
These are the decisions it cannot make. Each item gives our working
assumption; the code follows that assumption until counsel answers.

## Roles and contracts

1. **Controller / processor split.** We assume each brokerage is the
   controller of its CRM data and Neoh is its processor (CCPA "service
   provider"; VA/DE/TX "processor"), and that Neoh is the controller of
   accounts, billing and security logs. Confirm.
2. **Customer terms / DPA.** The brokerage agreement needs the processor
   terms:
   - CCPA §7051's nine required terms;
   - VA, DE and TX processor-contract terms;
   - delete-or-return at the end of service (we offer an export plus 30-day
     grace, then erasure);
   - the subprocessor list and change notice;
   - breach notice (we plan 10 days, to meet Maryland).

   We have no executed template yet.
3. **Applicability.** At launch Neoh is likely below the CCPA thresholds and
   may qualify for Texas TDPSA's small-business exemption. Delaware HB 380
   lowers the DPDPA thresholds from 2027-01-01. Confirm which laws bind
   Neoh directly, and which only through the brokerages.
4. **FTC Safeguards Rule.** Does the marketplace or financing surface make
   Neoh a "financial institution" (finder)? If so, there is a written
   information security programme and a two-year disposal rule.

## Retention periods (all marked counsel in the policy)

5. **Message bodies, transcripts, AI chat: no Neoh timer.** These are kept
   as the brokerage's records (TREC 22 TAC §535.2 four years, MD five, DE
   and PA three) until it deletes them or closes. Is "the brokerage decides"
   acceptable, or should Neoh impose an outer limit?
6. **Consent evidence and opt-outs: 5 years after the last event**, as keyed
   hashes after erasure. This covers 47 CFR 64.1200(d)(6) (internal DNC, 5
   years), the TCPA's 4-year limitation period, and TSR records (5 years). Is
   a hash sufficient evidence of an honoured opt-out in litigation?
7. **Billing: 7 years, then anonymised** (IRS). Is a shorter period
   acceptable for Stripe-held records?
8. **Security audit log: 2 years** (minimum 1). It contains agents' emails
   and IPs. Is that proportionate?
9. **Privacy records and receipts: 7 years.** Is that proportionate?
10. **Call recordings: 30 days** (carrier-side). Neoh stores none itself.
    Any brokerage obligation to keep recordings?

## Erasure and requests

11. **Requests touching transaction records.** A deletion request for a
    person who appears in a transaction is refused, and nothing is deleted.
    Confirm that broker-record retention overrides the deletion right here,
    and what partial anonymisation, if any, is required.
12. **Backups.** Deleted data stays in immutable 7-day backups and is
    re-erased after any restore. Confirm this satisfies §7022's backup
    exception and its VA, DE and TX equivalents.
13. **Response deadlines.** We plan for 45 days, plus 45 more on notice.
    Does DE HB 380 introduce a 30-day access deadline from 2027?
14. **Free-text mentions.** Discovery is by email and phone. Names inside
    notes and chat are not found automatically, and every result says so.
    Is the brokerage's manual search an acceptable "reasonable effort"?
15. **Identity verification.** Verifying the requester is left to the
    brokerage. Confirm that Neoh, as processor, has no duty here.

## Telephony and AI

16. **Recording and AI disclosure.** We treat DE, PA, MD and CA as all-party
    consent states, and disclose at the start of a call. Confirm the
    script, and whether an AI voice needs separate disclosure (some states).
17. **Voice AI in Singapore.** DashScope's default region is Singapore. Can
    we disclose cross-border processing, or must we re-region to the US
    before any real call?
18. **ElevenLabs training default.** We must opt out. Is the opt-out enough
    without an Enterprise contract?
19. **Voiceprints.** Texas CUBI requires destruction within one year after
    the purpose ends. We do not create voiceprints; confirm that TTS voice
    cloning (if ever enabled) counts.

## MLS

20. **Licence-termination purge.** We purge all licensed rows except
    listings a brokerage's own transaction references (Unlock MLS
    §29(b)/§35(b)). Does the brokerage's transaction file outweigh the purge
    clause? Are derived data (matches, comps) "copies" that must also go?
21. **Street View caching.** We cache for 30 days. Does Google's Maps terms
    forbid it?

## Breach

22. **Clocks.**
    - DE: 60 days.
    - TX: 60 days; the AG within 30 days for 250+ residents (the
      encryption safe harbour is weak).
    - MD: processor to owner within 10 days.
    - NJ: the State Police first.

    Confirm, and supply the notice templates.

"""Immutable NEOH platform-use policy data served to acceptance clients."""

from __future__ import annotations


# Bump this identifier whenever the policy text changes materially. Each user
# acceptance record is tied to this value so prior acknowledgement remains
# auditable rather than being silently reinterpreted under new wording.
PLATFORM_POLICY_VERSION = "neoh-platform-use-policy-2026-10-14-v4"

PLATFORM_POLICY_DOCUMENT = {
    "title": "NEOH™ Platform Use Policy",
    "operator": "NEOH™ is operated by YDN LLC.",
    "effective_date": "October 14, 2026",
    "introduction": (
        "This policy governs use of the NEOH™ platform. It is a platform-use "
        "policy, not legal, tax, financial, licensing, privacy, or fair-housing advice. "
        "Your brokerage and applicable law may impose additional requirements."
    ),
    "sections": [
        {
            "heading": "1. Account authority and responsible use",
            "paragraphs": [
                "Use NEOH™ only for lawful, professional real-estate work and only when you are authorized to act for your organization or client. You are responsible for activity performed through your account.",
                "Keep your account details accurate. Do not share credentials, use another person’s account, bypass access controls, or allow an unauthorized person to operate the platform on your behalf.",
            ],
        },
        {
            "heading": "2. Authorized data and confidentiality",
            "paragraphs": [
                "Enter, import, or connect only data you are entitled to access and use. Obtain any consent, notice, or authorization required for client, customer, lead, vendor, or teammate information.",
                "Protect confidential information and limit access to people with a legitimate business need. Do not submit authentication secrets, government identification numbers, payment-card data, bank credentials, or other sensitive information that is not necessary for the intended workflow.",
                "Each client and contact record is visible to the agent assigned to it, to your broker owner, and to anyone they share it with; a record with no assigned agent is visible to every agent in your brokerage. A contact you add for an assigned client stays with that client's agent. Share a record only with a colleague who has a legitimate business need, give edit access only when they need to change it, and remove access when the need ends.",
                "Texts, emails, calls, call notes and transcripts, generated documents, nurture plans and the outcome history linked to a client or contact follow that record: you see them only while you can see the record, and sending, calling, enrolling or generating a document for someone needs edit access, not a view-only share. Opt-outs, consent and contact-frequency limits always apply across your whole brokerage, whichever colleague's record they came from: if a person asks any of your agents to stop, every agent is blocked from contacting them.",
            ],
        },
        {
            "heading": "3. Fair housing and equal access",
            "paragraphs": [
                "Do not use NEOH™ to target, exclude, rank, steer, or make housing-related decisions based on protected characteristics or proxies for them. Do not use platform output as the sole basis for housing availability, credit, service, or eligibility decisions.",
                "You must keep meaningful human review over advertising, outreach, recommendations, and any decision that could affect a person’s housing opportunity or equal access.",
            ],
        },
        {
            "heading": "4. AI-assisted work and professional judgment",
            "paragraphs": [
                "AI-generated material is assistive and may be incomplete, inaccurate, or unsuitable for a particular transaction. Verify important facts against authoritative sources before relying on or communicating them.",
                "NEOH™ does not provide legal, tax, investment, appraisal, lending, title, or compliance advice. Obtain qualified professional review whenever your workflow, client, brokerage, or law requires it.",
                "Neoh takes notes on a call only after the person on the call agrees. Unless you choose to save them directly, those notes wait for your review, and Neoh does not rely on them until you approve them. Check AI call notes before you act on or share them.",
            ],
        },
        {
            "heading": "5. Contracts, templates, and approvals",
            "paragraphs": [
                "Use only the registered source-controlled template and version approved for the relevant workflow. A source-controlled template is not, by itself, an attorney-approved legal form.",
                "Do not treat a draft, redline, generated document, or platform status as a signed, binding, filed, or legally sufficient instrument. Required attorney review, brokerage approval, execution, and recordkeeping remain your responsibility.",
            ],
        },
        {
            "heading": "6. Communications and automation",
            "paragraphs": [
                "Use email, calling, messaging, task, and automation features only with the permissions, consent, disclosures, opt-out handling, and supervision required for the recipient and jurisdiction.",
                "Do not use NEOH™ to send deceptive, abusive, discriminatory, unsolicited, or unlawful communications, to impersonate another person, or to make an autonomous commitment for a client or brokerage.",
                "Do not remove, shorten, or talk over the disclosure Neoh gives at the start of an AI voice call, and do not tell the person they are speaking with a human.",
            ],
        },
        {
            "heading": "7. Security and platform integrity",
            "paragraphs": [
                "Do not probe, disrupt, reverse engineer, introduce malicious code, scrape outside permitted interfaces, or attempt to access data, tenants, accounts, or systems you are not authorized to access.",
                "Promptly report suspected unauthorized access, credential exposure, data loss, or misuse through your organization’s designated support and incident process. Cooperate with reasonable remediation steps needed to protect affected data and users.",
            ],
        },
        {
            "heading": "8. Your records and compliance responsibilities",
            "paragraphs": [
                "You remain responsible for your client relationships, records, disclosures, licensing obligations, supervision, retention, and decisions. Configure and use NEOH™ in accordance with your brokerage policy and applicable law.",
                "Do not represent platform content as independently verified, professional advice, or a guarantee of an outcome unless you have separately established that claim is accurate and permitted.",
            ],
        },
        {
            "heading": "9. Enforcement and policy changes",
            "paragraphs": [
                "YDN LLC may restrict or suspend access when use threatens people, data, platform integrity, or compliance. Material policy revisions will be published with a new version and may require a new acknowledgement before access continues.",
                "If you do not agree to this policy, do not use NEOH™ and sign out. Questions about your legal or compliance obligations should be directed to qualified counsel or your brokerage’s compliance lead.",
            ],
        },
    ],
}



# What the policy must keep saying (owner, 2026-10-13: "keep the text current").
# Each row ties a shipped capability to the evidence that it exists and to the
# words the policy uses for it. tests/test_policy_coverage.py fails when the
# evidence exists but the words are gone — and a NEW capability that changes who
# sees data, how people are contacted or what the AI does adds a row here, in
# the same change, with a version bump (CONTRACT C5.7).
POLICY_COVERAGE: tuple[dict[str, str], ...] = (
    {"capability": "per-record client visibility and sharing (D-2026-10-13-01)",
     "evidence": "db/migrations/0131_agent_record_scope.sql",
     "section": "2. Authorized data and confidentiality",
     "phrase": "anyone they share it with"},
    {"capability": "per-record contact visibility and sharing (D-2026-10-13-01, 0134)",
     "evidence": "db/migrations/0134_agent_contact_scope.sql",
     "section": "2. Authorized data and confidentiality",
     "phrase": "Each client and contact record is visible to the agent assigned to it"},
    {"capability": "communications follow the record; opt-outs brokerage-wide (D-2026-10-13-01, 0140)",
     "evidence": "db/migrations/0140_communications_follow_the_record.sql",
     "section": "2. Authorized data and confidentiality",
     "phrase": "if a person asks any of your agents to stop, every agent is blocked from contacting them"},
    # 0142 needs no new wording: a staged text, email or call is one of the
    # "texts, emails, calls" above, and sending one needs edit access.
    {"capability": "staged drafts and approvals follow the record (D-2026-10-13-01, 0142)",
     "evidence": "db/migrations/0142_drafts_follow_the_record.sql",
     "section": "2. Authorized data and confidentiality",
     "phrase": "sending, calling, enrolling or generating a document for someone needs edit access"},
    {"capability": "AI call notes: consent, review by default (D-2026-10-12-03)",
     "evidence": "call_note_settings.py",
     "section": "4. AI-assisted work and professional judgment",
     "phrase": "takes notes on a call only after the person on the call agrees"},
    {"capability": "AI voice disclosure on calls",
     "evidence": "outreach_compliance.py",
     "section": "6. Communications and automation",
     "phrase": "disclosure Neoh gives at the start of an AI voice call"},
)

# Separate account-security addendum for onboarding diagnostics.  This is a
# non-legal acknowledgment surface that focuses on platform hardening, access
# controls, and incident reporting.  The PolicyAcceptanceGate renders this when the
# policy status endpoint is unavailable so agents can still review account security
# obligations before retrying or signing out.
ACCOUNT_SECURITY_ESA_VERSION = "neoh-account-security-esa-2026-07-18-v1"

ACCOUNT_SECURITY_ESA_DOCUMENT = {
    "title": "NEOH™ Account Security Agreement (ESA)",
    "operator": "NEOH™ is operated by YDN LLC.",
    "effective_date": "July 18, 2026",
    "introduction": (
        "This account security addendum establishes the minimum security practices "
        "that must govern NEOH™ account access and use.  It is an account-security "
        "document for platform operation and does not replace legal, tax, or regulatory "
        "obligations under law."
    ),
    "sections": [
        {
            "heading": "1. Account integrity",
            "paragraphs": [
                "Use one account per person. Do not share credentials, session links, or temporary tokens, and do not authorize others to act on your behalf.",
                "Terminate device access immediately when credentials are exposed, a device is lost, or unauthorized access is suspected.",
            ],
        },
        {
            "heading": "2. Authentication and session hygiene",
            "paragraphs": [
                "Keep authentication material confidential. Use a unique, manager-approved password and rotate it immediately after suspected compromise.",
                "Use session controls on shared devices: lock the device, sign out on public terminals, and review active sessions when practical.",
            ],
        },
        {
            "heading": "3. Data access discipline",
            "paragraphs": [
                "Access only information necessary for assigned workflows. Do not export, print, or forward confidential client/property data without authority.",
                "Do not bypass tenant boundaries, impersonate another broker, or intentionally access records, accounts, or integrations outside approved roles.",
            ],
        },
        {
            "heading": "4. Integrity and abuse prevention",
            "paragraphs": [
                "Do not run automated scripts, brute-force tests, or unauthorized scraping against NEOH™ infrastructure.",
                "Report any malware signals, suspicious API behavior, abuse attempts, or unexpected data exports through the incident response process immediately.",
            ],
        },
        {
            "heading": "5. Compliance and enforcement",
            "paragraphs": [
                "YDN LLC may temporarily restrict, suspend, or revoke access when account-security obligations are violated or when investigation is required.",
                "If this agreement’s content materially changes, access may require a refreshed acknowledgement before protected operations resume.",
            ],
        },
    ],
}


def _render_policy_document(document: dict[str, str | list[dict[str, str]]]) -> str:
    """Render a policy/ESA dictionary into plain-text form body."""
    lines = [
        str(document.get("title") or "NEOH™ Account Security Agreement"),
        "",
        f"Effective Date: {document.get('effective_date')}",
        str(document.get("operator") or "NEOH™"),
        "",
    ]
    if document.get("introduction"):
        lines.extend([str(document["introduction"]), ""])

    for section in document.get("sections", []):
        heading = section.get("heading") if isinstance(section, dict) else None
        if heading:
            lines.append(str(heading))
        for paragraph in section.get("paragraphs", []):
            lines.append(str(paragraph))
        lines.append("")

    lines.append("This account-security agreement is operational guidance for platform use and not legal advice.")
    return "\n".join(lines).strip() + "\n"


def account_security_esa_pdf_text() -> str:
    """Return the canonical ESA body used by contract-workspace templates."""
    return _render_policy_document(ACCOUNT_SECURITY_ESA_DOCUMENT)

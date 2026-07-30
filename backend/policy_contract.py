"""Immutable NEOH platform-use policy data served to acceptance clients."""

from __future__ import annotations


# Bump this identifier whenever the policy text changes materially. Each user
# acceptance record is tied to this value so prior acknowledgement remains
# auditable rather than being silently reinterpreted under new wording.
PLATFORM_POLICY_VERSION = "neoh-platform-use-policy-2026-07-15-v1"

PLATFORM_POLICY_DOCUMENT = {
    "title": "NEOH™ Platform Use Policy",
    "operator": "NEOH™ is operated by YDN LLC.",
    "effective_date": "July 15, 2026",
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

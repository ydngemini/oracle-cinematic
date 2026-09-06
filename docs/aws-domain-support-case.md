# AWS Support case — unblock `neohrealestate.com` registration

`aws support create-case` returns `SubscriptionRequiredException` on this
account's plan, so this must be filed in the console. Basic support **does**
accept account/billing cases, which is the right category for this.

**Console:** https://console.aws.amazon.com/support/home#/case/create
**Type:** Account and billing → **Service: Registrar** → **Category: Domain registration**
**Severity:** General guidance

## Subject

    Domain registration fails immediately — neohrealestate.com (available), account 151105438863

## Body

    Route 53 Domains registration for neohrealestate.com fails within one second
    of submission, with no actionable detail. It has failed twice:

      OperationId  05990a03-58a2-4074-b497-1181f0559ad6
      Submitted    2026-08-28T16:53:21Z
      Status       FAILED
      Message      "We can't finish registering your domain. Contact AWS Support
                    at ... for further information."

      Second attempt, same domain: 2026-08-30T04:29:00Z — also FAILED.

    check-domain-availability reports the domain as AVAILABLE, so this is not a
    naming conflict:

      $ aws route53domains check-domain-availability \
          --domain-name neohrealestate.com --region us-east-1
      { "Availability": "AVAILABLE" }

    The account (151105438863) is ACTIVE and was created 2026-08-27. I believe
    registration is being held pending new-account identity or payment
    verification, but the API gives no way to see or clear that.

    Please advise what verification is outstanding, or enable domain
    registration on the account.

    This is blocking a production deployment: the Terraform stack resolves the
    hosted zone for this domain as a data source, so nothing can be applied
    until the domain is registered and its zone exists.

## While waiting

Everything else is verified ready — see `docs/production-blockers.md`.

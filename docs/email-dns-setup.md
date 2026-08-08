# Email setup

Neoh sends mail over plain SMTP, through a server **you** name. There is no
third-party email API in the path and no default host — `ORACLE_SMTP_HOST` must
be set explicitly, because guessing a provider is how a deployment ends up
silently routing mail through someone else's service.

Three independent paths exist:

| Path | What it sends | Configured by |
|---|---|---|
| **Platform SMTP** | password reset, approved outbound mail | `ORACLE_SMTP_*` below |
| **Tenant BYO SMTP** | a brokerage's own outbound mail | the encrypted `smtp` provider vault, per tenant |
| **ACS / SES** | alternatives to the above | `ORACLE_EMAIL_PROVIDER=acs\|ses` |

## App configuration

```bash
ORACLE_EMAIL_PROVIDER=smtp            # default

ORACLE_SMTP_HOST=                     # required — no default
ORACLE_SMTP_PORT=587                  # STARTTLS; 465 for implicit TLS
ORACLE_SMTP_USERNAME=
ORACLE_SMTP_PASSWORD=                 # secret reference, never a literal
ORACLE_SMTP_FROM_EMAIL=no-reply@yourdomain
ORACLE_SMTP_FROM_NAME=Neoh
```

The transport is always encrypted. Port 465 is implicit TLS; every other port
must negotiate STARTTLS or the send is **abandoned rather than downgraded** — a
password reset link is never put on the wire in the clear
(`backend/smtp_mailer.py`).

Authentication failures are reported as *configuration* errors rather than send
failures, so a wrong or expired credential is unambiguous in the logs.

Tenants who run their own mail server store credentials against the `smtp`
provider; those are validated by a real connect-and-authenticate before being
trusted, and override the platform values for that tenant.

## DNS: the three records any sending domain needs

Independent of which server sends, receivers judge mail on the **`From:`
domain's** DNS. All three records go wherever that domain's DNS is hosted.

### SPF — one TXT record at the apex

```
TXT  @   v=spf1 ip4:<your mail server IP> ~all
```

Use `ip4:` / `ip6:` for a server you run. If you relay through a provider, use
their published `include:` instead. Either way:

**Never end up with two SPF records.** Two `v=spf1` TXT records is a permanent
`permerror` — receivers treat it as no SPF at all. It is not a merge. If one
exists, edit it; add further senders as extra mechanisms inside the same string,
before `~all`.

Keep `~all` (softfail) until DMARC reports are clean, then consider `-all`.

### DKIM — a TXT record at your selector

A self-hosted server signs with OpenDKIM or its built-in signer; you publish the
public half at `<selector>._domainkey`. The selector name is your choice.

```
TXT  <selector>._domainkey   v=DKIM1; k=rsa; p=<public key>
```

Use a 2048-bit key. If the DNS panel rejects the long value, split it into
quoted chunks — DNS concatenates them.

### DMARC — a TXT record at `_dmarc`, added last

```
TXT  _dmarc   v=DMARC1; p=none; rua=mailto:dmarc@yourdomain; fo=1
```

Start at `p=none`. It changes no delivery behaviour and only collects reports.
Going straight to `p=reject` before reading those reports is the standard way to
make legitimate mail disappear silently. Tighten to `quarantine`, then `reject`,
after a couple of clean weeks. `rua` needs a mailbox you can actually read.

## Current state of `ydnhft.com` (checked 2026-08-07)

This domain was verified during setup and still has gaps that affect mail
**already being sent from it**, regardless of Neoh:

```
MX     : 1 smtp.google.com.                         present
DKIM   : google._domainkey → v=DKIM1;k=rsa;p=…      present
SPF    : (none)                                      MISSING
DMARC  : _dmarc → NXDOMAIN                           MISSING
```

DNS for it is at **IONOS** (`ns1022.ui-dns.com` and siblings). If you keep using
that domain as the `From:` address, the SPF and DMARC records above still need
adding. If you move to a self-hosted mail server, the MX and DKIM records change
to match it and SPF must list your server rather than a relay.

## Verifying

```bash
dig +short TXT yourdomain                       # exactly one v=spf1 line
dig +short TXT <selector>._domainkey.yourdomain # DKIM public key
dig +short TXT _dmarc.yourdomain                # DMARC policy
dig +short MX  yourdomain                       # only if hosting mailboxes
```

Then trigger a real password reset to an external address and open the received
message's original headers. `SPF`, `DKIM` and `DMARC` should each read `PASS`.
Allow up to the TTL before concluding a record is wrong.

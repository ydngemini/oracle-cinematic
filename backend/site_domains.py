"""DNS evidence for a site's custom domain (prompt 14, T14.04, AC14.04).

A publish that names a hostname creates a *claim* (one ``hyperlocal_site_domains``
row, globally unique by hostname) with a 256-bit token PostgreSQL generates
(0173). To prove control of the name, its owner adds one TXT record:

    _neoh-site-verify.<hostname>   TXT   "neoh-site-verify=<token>"

The token is single-purpose: it belongs to that one claim (brokerage, site and
hostname) and is only ever looked for at that exact name. It is not a secret
(it is published in DNS by design); what matters is that nobody can predict it
before the claim is made.

What DNS answered decides the next state (``decide``); nothing else does:

    TXT matches the claim's token          -> verified
      ... and the operator has named the platform's serving target
          (ORACLE_SITES_DOMAIN_TARGET), the site is published and the
          hostname's CNAME is that target  -> active (a pending or failed
          claim is written verified first: ``steps``)
    no challenge record (NXDOMAIN, no      -> pending stays pending, failed
      answer, or only TXT values that are     stays failed, verified/active
      not "neoh-site-verify=...", such as     -> failed (the proof is gone)
      a wildcard SPF record)
    a "neoh-site-verify=" value that is    -> failed
      not this claim's (stale or foreign)

``active`` therefore means: ownership is proven and the name points at the
target the operator declared for custom-hostname serving. It is not a claim
that the edge serves TLS for the name, or that anything dispatches a request
for it to the site: neither exists in this repository today, and both are
operator and platform work (``.env.prod.example``, ORACLE_SITES_DOMAIN_TARGET).

A resolver that times out, SERVFAILs or cannot be reached is *unknown*, never
failure and never success: ``DnsUnavailable`` is raised, the route writes
nothing and answers "retryable". Every lookup is bounded by
ORACLE_DNS_TIMEOUT_SECONDS (default 4, clamped to 1-10) inside dnspython and
again by ``asyncio.wait_for``, so a check costs at most two bounded lookups.

Tests pass a stub resolver; nothing here is called with a real network in CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

log = logging.getLogger(__name__)

CHALLENGE_LABEL = "_neoh-site-verify"
CHALLENGE_PREFIX = "neoh-site-verify="
RRType = Literal["TXT", "CNAME"]

_DEFAULT_TIMEOUT = 4.0
_MIN_TIMEOUT, _MAX_TIMEOUT = 1.0, 10.0


class DnsUnavailable(Exception):
    """DNS could not give an answer (timeout, SERVFAIL, unreachable): unknown."""


class DnsResolver(Protocol):
    async def lookup(self, name: str, rdtype: RRType) -> list[str]:
        """Answers at ``name``; ``[]`` is a definitive negative (NXDOMAIN or
        no record of that type). Raises ``DnsUnavailable`` when unknown."""


def challenge_name(hostname: str) -> str:
    return f"{CHALLENGE_LABEL}.{hostname}"


def challenge_value(token: str) -> str:
    return f"{CHALLENGE_PREFIX}{token}"


def challenge(hostname: str, token: str) -> dict[str, str]:
    return {"type": "TXT", "name": challenge_name(hostname), "value": challenge_value(token)}


def dns_timeout_seconds() -> float:
    raw = (os.getenv("ORACLE_DNS_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_TIMEOUT
    except ValueError:
        return _DEFAULT_TIMEOUT
    if value != value:  # NaN
        return _DEFAULT_TIMEOUT
    return max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, value))


def _nameservers() -> list[str]:
    servers = []
    for item in (os.getenv("ORACLE_DNS_NAMESERVERS") or "").split(","):
        item = item.strip()
        try:
            servers.append(str(ipaddress.ip_address(item)))
        except ValueError:
            continue
    return servers


def _hostname_or_none(value: str) -> Optional[str]:
    candidate = value.strip().rstrip(".").lower()
    labels = candidate.split(".")
    if len(candidate) > 253 or len(labels) < 2:
        return None
    for label in labels:
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-":
            return None
        if not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label):
            return None
    return candidate


def serving_target() -> Optional[str]:
    """The hostname custom domains must CNAME to, as the operator declared it
    when the edge serves custom hostnames. Unset (the default): no claim can be
    ``active``; ownership can still be verified."""
    return _hostname_or_none(os.getenv("ORACLE_SITES_DOMAIN_TARGET") or "")


class DnspythonResolver:
    """Asynchronous lookups through dnspython (ISC licence), never on a thread
    and never blocking the event loop."""

    def __init__(self, timeout: Optional[float] = None, nameservers: Optional[list[str]] = None):
        self.timeout = dns_timeout_seconds() if timeout is None else timeout
        self.nameservers = nameservers

    async def lookup(self, name: str, rdtype: RRType) -> list[str]:
        try:
            import dns.asyncresolver
            import dns.exception
            import dns.resolver
        except ImportError as exc:  # requirements.txt pins it; a broken image is unknown, not failure
            log.error("dnspython is not installed; DNS checks cannot run")
            raise DnsUnavailable("resolver library missing") from exc

        servers = self.nameservers if self.nameservers is not None else _nameservers()
        try:
            # configure=True reads the system resolver configuration and can
            # itself fail (NoResolverConfiguration): unknown, like a timeout.
            resolver = dns.asyncresolver.Resolver(configure=not servers)
            if servers:
                resolver.nameservers = list(servers)
            answer = await asyncio.wait_for(
                resolver.resolve(name, rdtype, lifetime=self.timeout, search=False, raise_on_no_answer=False),
                self.timeout + 0.5,
            )
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except (asyncio.TimeoutError, OSError) as exc:
            raise DnsUnavailable(type(exc).__name__) from exc
        except dns.exception.DNSException as exc:
            # Timeout, NoNameservers (SERVFAIL everywhere), no resolver
            # configuration, malformed answers: none of them is evidence.
            raise DnsUnavailable(type(exc).__name__) from exc
        rrset = getattr(answer, "rrset", None)
        if not rrset:
            return []
        if rdtype == "TXT":
            return [b"".join(rdata.strings).decode("utf-8", "replace") for rdata in rrset]
        return [rdata.target.to_text().rstrip(".").lower() for rdata in rrset]


def get_dns_resolver() -> DnsResolver:
    """FastAPI dependency; tests pass a stub instead."""
    return DnspythonResolver()


@dataclass(frozen=True)
class Observation:
    """What DNS answered, reduced to the verdicts the decision needs.

    ``txt``: matched | missing | mismatch. ``cname``: matched | missing |
    mismatch, or None when it was not looked up (no target, or no TXT match).
    Only the hash of the matched value is kept; other TXT values at the name
    are counted, never stored."""

    txt: str
    cname: Optional[str]
    matched_sha256: Optional[str]
    evidence: dict[str, Any] = field(default_factory=dict)


async def check_domain(
    resolver: DnsResolver, *, hostname: str, token: str, target: Optional[str]
) -> Observation:
    """Look up the claim's TXT record (and its CNAME when it matters).
    Raises ``DnsUnavailable`` when DNS gave no answer either way."""
    name = challenge_name(hostname)
    expected = challenge_value(token)
    values = await resolver.lookup(name, "TXT")
    matched = next(
        (value for value in values if hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))),
        None,
    )
    # Only a challenge-shaped value is evidence either way: a wildcard TXT on
    # the zone (an SPF record, say) also answers at the challenge name, and it
    # says nothing about this claim.
    challenges = [value for value in values if value.startswith(CHALLENGE_PREFIX)]
    txt = "matched" if matched is not None else ("mismatch" if challenges else "missing")
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest() if matched is not None else None
    evidence: dict[str, Any] = {"record": name, "rdtype": "TXT", "answer_count": len(values), "txt": txt}
    if digest:
        evidence["matched_sha256"] = digest
    cname: Optional[str] = None
    if txt == "matched" and target:
        targets = await resolver.lookup(hostname, "CNAME")
        cname = "matched" if target in targets else ("mismatch" if targets else "missing")
        evidence["cname"] = {"answer_count": len(targets), "verdict": cname}
    return Observation(txt=txt, cname=cname, matched_sha256=digest, evidence=evidence)


_LIVE_PROOF = ("verified", "active")


def decide(
    current: str, *, txt: str, cname: Optional[str], site_published: bool, target_configured: bool
) -> tuple[str, str]:
    """(next status, outcome) for a claim in ``current`` given what DNS said."""
    if current not in ("pending", "verified", "active", "failed"):
        raise ValueError(f"a {current} domain is not checked")
    if txt == "matched":
        if target_configured and site_published and cname == "matched":
            return "active", "active"
        return "verified", "verified"
    if txt == "missing":
        return ("failed" if current in _LIVE_PROOF else current), "record_not_found"
    return "failed", "record_mismatch"


def steps(current: str, status: str) -> list[str]:
    """The statuses to write, in order, to take a claim from ``current`` to the
    ``status`` that ``decide`` chose, using only moves the 0173 trigger allows.
    ``active`` is reachable only from ``verified``: a pending or failed claim
    whose one check matched both the TXT and the CNAME is written ``verified``
    and then ``active`` in the same transaction, each move in the trail."""
    if status == "active" and current not in _LIVE_PROOF:
        return ["verified", "active"]
    return [status]


def outcome_message(outcome: str, *, status: str, previous: str, target: Optional[str], site_published: bool) -> str:
    """One plain sentence for the person who pressed "Check DNS"."""
    if outcome == "active":
        return "Ownership verified, and this address points at the platform."
    if outcome == "verified":
        if not target:
            return "Ownership verified. Serving the site at this address is not set up on this platform yet."
        if not site_published:
            return "Ownership verified. Publish the site before this address can be used."
        return f"Ownership verified. To use this address, point a CNAME record for it at {target}."
    if outcome == "record_not_found":
        if previous in _LIVE_PROOF:
            return "The verification record is gone, so this domain is no longer verified."
        return ("No verification record was found yet. DNS changes can take a while to appear; "
                "check again later.")
    if outcome == "record_mismatch":
        return "A verification record exists, but its value does not match this site. Use the value shown."
    return "DNS could not be reached. Nothing changed — try again in a moment."

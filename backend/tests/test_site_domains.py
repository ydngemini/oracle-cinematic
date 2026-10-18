"""A site's custom domain moves on DNS evidence only, and an unreachable
resolver changes nothing (prompt 14, T14.04, AC14.04).

No test here makes a real DNS or network call: every resolver is a stub, and
the dnspython adapter is driven through a fake ``dns.asyncresolver``.

Pins:
  * the challenge is a TXT record at ``_neoh-site-verify.<hostname>`` whose
    value names this claim's own token, compared in constant time;
  * the next state is decided from what DNS answered: a matching TXT verifies,
    a missing one leaves a pending claim pending and fails a verified one, a
    foreign or stale value fails; ``active`` needs the operator's serving
    target, a published site and a CNAME onto that target;
  * every write a check makes is a move the 0173 trigger allows (read from
    the migration itself): a pending or failed claim never jumps to
    ``active``, it is written ``verified`` and then ``active`` (``steps``);
  * a timeout, SERVFAIL or a resolver that never answers is ``DnsUnavailable``
    (unknown, not failure), bounded by the configured timeout;
  * the hostname a publish may claim is never the platform's own and its
    challenge name fits in DNS.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import re
import sys
import time
import types
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

import site_domains as sd

TOKEN = "ab" * 32
HOST = "homes.example.com"


class StubResolver:
    def __init__(self, txt=None, cname=None, fail=None):
        self.txt, self.cname, self.fail = txt or [], cname or [], fail
        self.calls: list[tuple[str, str]] = []

    async def lookup(self, name, rdtype):
        self.calls.append((name, rdtype))
        if self.fail == rdtype or self.fail == "all":
            raise sd.DnsUnavailable("stub")
        return list(self.txt if rdtype == "TXT" else self.cname)


def _check(resolver, target=None, token=TOKEN):
    return asyncio.run(sd.check_domain(resolver, hostname=HOST, token=token, target=target))


# ── the challenge ────────────────────────────────────────────────────────────

def test_the_challenge_is_a_txt_record_naming_this_claims_token():
    assert sd.challenge(HOST, TOKEN) == {
        "type": "TXT", "name": f"_neoh-site-verify.{HOST}", "value": f"neoh-site-verify={TOKEN}"}


def test_a_matching_record_is_found_among_others_and_only_its_hash_is_kept():
    other = "v=spf1 include:mail.example.net ~all"
    resolver = StubResolver(txt=[other, f"neoh-site-verify={TOKEN}"])
    observed = _check(resolver)
    assert observed.txt == "matched"
    assert resolver.calls == [(f"_neoh-site-verify.{HOST}", "TXT")]  # no CNAME without a target
    expected = hashlib.sha256(f"neoh-site-verify={TOKEN}".encode()).hexdigest()
    assert observed.matched_sha256 == expected
    assert observed.evidence["matched_sha256"] == expected
    assert observed.evidence["answer_count"] == 2
    assert other not in str(observed.evidence)          # other TXT values are never stored


@pytest.mark.parametrize("values, verdict", [
    ([], "missing"),
    ([f"neoh-site-verify={'cd' * 32}"], "mismatch"),       # another claim's (or a stale) token
    ([f"neoh-site-verify={TOKEN} "], "mismatch"),          # not exactly this value
    # Not a challenge record at all (a wildcard TXT answers every name): no
    # evidence either way, so a pending claim is not failed by someone's SPF.
    (["v=spf1 include:mail.example.net ~all"], "missing"),
    ([TOKEN], "missing"),
])
def test_a_missing_or_foreign_record_is_not_a_match(values, verdict):
    assert _check(StubResolver(txt=values)).txt == verdict


def test_the_cname_is_read_only_when_the_operator_named_a_serving_target():
    resolver = StubResolver(txt=[f"neoh-site-verify={TOKEN}"], cname=["sites.neoh.example"])
    observed = _check(resolver, target="sites.neoh.example")
    assert observed.cname == "matched"
    assert resolver.calls[-1] == (HOST, "CNAME")
    assert _check(StubResolver(txt=[f"neoh-site-verify={TOKEN}"], cname=["elsewhere.example"]),
                  target="sites.neoh.example").cname == "mismatch"
    assert _check(StubResolver(txt=[f"neoh-site-verify={TOKEN}"]), target="sites.neoh.example").cname == "missing"
    # No TXT match, no CNAME lookup: the address is never examined for an unproven claim.
    resolver = StubResolver(txt=[], cname=["sites.neoh.example"])
    assert _check(resolver, target="sites.neoh.example").cname is None
    assert [rd for _, rd in resolver.calls] == ["TXT"]


@pytest.mark.parametrize("fail", ["TXT", "CNAME"])
def test_an_unreachable_resolver_is_unknown_not_failure(fail):
    resolver = StubResolver(txt=[f"neoh-site-verify={TOKEN}"], cname=["sites.neoh.example"], fail=fail)
    with pytest.raises(sd.DnsUnavailable):
        _check(resolver, target="sites.neoh.example")


# ── the decision ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("current, txt, cname, published, target, expected", [
    # A matching TXT verifies, from any non-terminal state.
    ("pending", "matched", None, True, False, ("verified", "verified")),
    ("failed", "matched", None, True, False, ("verified", "verified")),
    ("verified", "matched", None, True, False, ("verified", "verified")),
    # active needs the target, a published site and the CNAME, all three.
    ("verified", "matched", "matched", True, True, ("active", "active")),
    ("pending", "matched", "matched", True, True, ("active", "active")),     # written through verified
    ("failed", "matched", "matched", True, True, ("active", "active")),      # (``steps``)
    ("verified", "matched", "matched", False, True, ("verified", "verified")),
    ("verified", "matched", "mismatch", True, True, ("verified", "verified")),
    ("active", "matched", "missing", True, True, ("verified", "verified")),
    # No record: a pending claim waits; proven ownership is lost.
    ("pending", "missing", None, True, True, ("pending", "record_not_found")),
    ("failed", "missing", None, True, True, ("failed", "record_not_found")),
    ("verified", "missing", None, True, True, ("failed", "record_not_found")),
    ("active", "missing", None, True, True, ("failed", "record_not_found")),
    # A record that names something else fails the claim.
    ("pending", "mismatch", None, True, True, ("failed", "record_mismatch")),
    ("active", "mismatch", None, True, True, ("failed", "record_mismatch")),
])
def test_the_next_state_follows_the_evidence(current, txt, cname, published, target, expected):
    assert sd.decide(current, txt=txt, cname=cname, site_published=published, target_configured=target) == expected


def test_no_evidence_path_reaches_active_without_a_matching_txt():
    for current in ("pending", "failed", "verified", "active"):
        for txt in ("missing", "mismatch"):
            for cname in (None, "matched"):
                status, _ = sd.decide(current, txt=txt, cname=cname, site_published=True, target_configured=True)
                assert status not in ("verified", "active"), (current, txt, cname)


def _trigger_moves() -> set[tuple[str, str]]:
    """The (from, to) pairs the 0173 transition trigger allows, read from the
    migration itself so this cannot drift from what the database enforces."""
    sql = (Path(__file__).parents[1] / "db" / "migrations" / "0173_site_domains_and_rollback.sql").read_text()
    body = sql[sql.index("FUNCTION hyperlocal_site_domains_check_transition"):]
    body = body[body.index("NOT IN ("):body.index(") THEN")]
    moves = set(re.findall(r"\('(\w+)', '(\w+)'\)", body))
    assert ("verified", "active") in moves and ("pending", "active") not in moves
    return moves


@pytest.mark.parametrize("current, target, expected", [
    ("pending", "active", ["verified", "active"]),
    ("failed", "active", ["verified", "active"]),
    ("verified", "active", ["active"]),
    ("active", "active", ["active"]),           # same status: one write records the check
    ("pending", "pending", ["pending"]),
    ("pending", "verified", ["verified"]),
    ("active", "failed", ["failed"]),
])
def test_a_claim_reaches_active_only_through_verified(current, target, expected):
    assert sd.steps(current, target) == expected


def test_every_write_a_check_makes_is_a_move_the_database_allows():
    moves = _trigger_moves()
    for current, txt, cname, published, target in itertools.product(
            ("pending", "failed", "verified", "active"), ("matched", "missing", "mismatch"),
            (None, "matched", "mismatch", "missing"), (True, False), (True, False)):
        status, _ = sd.decide(current, txt=txt, cname=cname, site_published=published, target_configured=target)
        path = sd.steps(current, status)
        assert path and path[-1] == status, (current, status, path)
        for before, after in zip([current, *path], path):
            assert before == after or (before, after) in moves, (current, txt, cname, published, target, path)


def test_a_retired_claim_is_never_decided():
    with pytest.raises(ValueError):
        sd.decide("retired", txt="matched", cname=None, site_published=True, target_configured=False)


# ── configuration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [(None, 4.0), ("", 4.0), ("0", 1.0), ("2.5", 2.5), ("90", 10.0),
                                           ("nope", 4.0)])
def test_the_dns_timeout_is_bounded(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ORACLE_DNS_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ORACLE_DNS_TIMEOUT_SECONDS", raw)
    assert sd.dns_timeout_seconds() == expected


def test_the_serving_target_is_a_normalised_hostname_or_nothing(monkeypatch):
    monkeypatch.delenv("ORACLE_SITES_DOMAIN_TARGET", raising=False)
    assert sd.serving_target() is None
    monkeypatch.setenv("ORACLE_SITES_DOMAIN_TARGET", " Sites.Neoh.Example. ")
    assert sd.serving_target() == "sites.neoh.example"
    monkeypatch.setenv("ORACLE_SITES_DOMAIN_TARGET", "https://not a host/")
    assert sd.serving_target() is None


# ── the dnspython adapter, through a fake dns package ────────────────────────

class _Rdata:
    def __init__(self, strings=None, target=None):
        self.strings, self.target = strings, target


class _Name:
    def __init__(self, text):
        self.text = text

    def to_text(self):
        return self.text


def _fake_dns(monkeypatch, behaviour):
    """Install a fake ``dns`` package whose Resolver.resolve runs ``behaviour``."""
    exception = types.ModuleType("dns.exception")

    class DNSException(Exception):
        pass

    class Timeout(DNSException):
        pass

    exception.DNSException, exception.Timeout = DNSException, Timeout
    resolver_mod = types.ModuleType("dns.resolver")

    class NXDOMAIN(DNSException):
        pass

    class NoAnswer(DNSException):
        pass

    class NoNameservers(DNSException):
        pass

    class LifetimeTimeout(Timeout):
        pass

    class NoResolverConfiguration(DNSException):
        pass

    for cls in (NXDOMAIN, NoAnswer, NoNameservers, LifetimeTimeout, NoResolverConfiguration):
        setattr(resolver_mod, cls.__name__, cls)
    seen: dict = {}

    class Resolver:
        def __init__(self, configure=True):
            seen["configure"] = configure
            self.nameservers = []

        async def resolve(self, name, rdtype, **kwargs):
            seen.update(name=name, rdtype=rdtype, kwargs=kwargs, nameservers=list(self.nameservers))
            return await behaviour(resolver_mod, exception)

    asyncresolver = types.ModuleType("dns.asyncresolver")
    asyncresolver.Resolver = Resolver
    package = types.ModuleType("dns")
    package.exception, package.resolver, package.asyncresolver = exception, resolver_mod, asyncresolver
    for name, module in (("dns", package), ("dns.exception", exception), ("dns.resolver", resolver_mod),
                         ("dns.asyncresolver", asyncresolver)):
        monkeypatch.setitem(sys.modules, name, module)
    return seen


def test_the_adapter_reads_txt_and_cname_answers(monkeypatch):
    async def txt(resolver_mod, exception):
        return types.SimpleNamespace(rrset=[_Rdata(strings=[b"neoh-site-verify=", b"abc"]),
                                            _Rdata(strings=[b"other"])])

    seen = _fake_dns(monkeypatch, txt)
    resolver = sd.DnspythonResolver(timeout=2.0)
    assert asyncio.run(resolver.lookup("_neoh-site-verify.homes.example.com", "TXT")) == [
        "neoh-site-verify=abc", "other"]
    assert seen["kwargs"]["lifetime"] == 2.0 and seen["kwargs"]["search"] is False
    assert seen["kwargs"]["raise_on_no_answer"] is False

    async def cname(resolver_mod, exception):
        return types.SimpleNamespace(rrset=[_Rdata(target=_Name("Sites.Neoh.Example."))])

    _fake_dns(monkeypatch, cname)
    assert asyncio.run(sd.DnspythonResolver(timeout=2.0).lookup(HOST, "CNAME")) == ["sites.neoh.example"]


def test_the_adapter_uses_only_the_configured_nameservers(monkeypatch):
    async def empty(resolver_mod, exception):
        return types.SimpleNamespace(rrset=None)

    seen = _fake_dns(monkeypatch, empty)
    monkeypatch.setenv("ORACLE_DNS_NAMESERVERS", "192.0.2.53, not-an-ip, 2001:db8::53")
    assert asyncio.run(sd.DnspythonResolver(timeout=1.0).lookup(HOST, "TXT")) == []
    assert seen["configure"] is False and seen["nameservers"] == ["192.0.2.53", "2001:db8::53"]


@pytest.mark.parametrize("raised, outcome", [
    ("NXDOMAIN", []), ("NoAnswer", []),
    ("LifetimeTimeout", "unavailable"), ("NoNameservers", "unavailable"),
    ("NoResolverConfiguration", "unavailable"), ("OSError", "unavailable"),
])
def test_the_adapter_maps_negative_answers_and_failures(monkeypatch, raised, outcome):
    async def behaviour(resolver_mod, exception):
        if raised == "OSError":
            raise OSError("network unreachable")
        raise getattr(resolver_mod, raised)()

    _fake_dns(monkeypatch, behaviour)
    resolver = sd.DnspythonResolver(timeout=1.0)
    if outcome == "unavailable":
        with pytest.raises(sd.DnsUnavailable):
            asyncio.run(resolver.lookup(HOST, "TXT"))
    else:
        assert asyncio.run(resolver.lookup(HOST, "TXT")) == outcome


def test_a_resolver_that_never_answers_is_cut_off_by_the_timeout(monkeypatch):
    async def hang(resolver_mod, exception):
        await asyncio.sleep(30)

    _fake_dns(monkeypatch, hang)
    started = time.monotonic()
    with pytest.raises(sd.DnsUnavailable):
        asyncio.run(sd.DnspythonResolver(timeout=0.05).lookup(HOST, "TXT"))
    assert time.monotonic() - started < 2


# ── what a publish may claim ─────────────────────────────────────────────────

def test_a_publish_cannot_claim_the_platforms_own_hostname(monkeypatch):
    import sites_api

    monkeypatch.setenv("ORACLE_BASE_URL", "https://app.neoh.example")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://api.neoh.example")
    monkeypatch.setenv("ORACLE_SITES_DOMAIN_TARGET", "sites.neoh.example")
    for hostname in ("app.neoh.example", "x.app.neoh.example", "API.neoh.example", "sites.neoh.example"):
        with pytest.raises(ValidationError, match="platform"):
            sites_api.SitePublishRequest(revision_id=uuid.uuid4(), hostname=hostname)
    assert sites_api.SitePublishRequest(revision_id=uuid.uuid4(), hostname="homes.example.com").hostname == HOST


def test_a_publish_cannot_claim_a_hostname_whose_challenge_does_not_fit_in_dns():
    import sites_api

    longest = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 40]) + ".com"     # 236 characters
    assert len(longest) == 236 and len("_neoh-site-verify." + longest) == 254
    with pytest.raises(ValidationError, match="too long"):
        sites_api.SitePublishRequest(revision_id=uuid.uuid4(), hostname=longest)
    fits = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 39]) + ".com"        # 235: challenge name is 253
    assert len("_neoh-site-verify." + fits) == 253
    assert sites_api.SitePublishRequest(revision_id=uuid.uuid4(), hostname=fits).hostname == fits

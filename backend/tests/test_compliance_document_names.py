"""Every required document on the state checklist must be identifiable.

The defect this pins down: `compliance_document_prelist` named every document
`rule.title`, but a single rule can require several genuinely different
documents. FEDERAL-RESPA-001 requires BOTH a Loan Estimate and a Closing
Disclosure (one due 3 business days AFTER application, the other 3 business days
BEFORE closing); FEDERAL-LEAD-PAINT-001 requires the disclosure AND the EPA
pamphlet. All of them rendered as one repeated row, so an agent could mark one
delivered believing the other was covered — on a compliance surface that is a
missed statutory deadline, not a cosmetic bug.

The fix must not overcorrect: a rule requiring ONE document keeps its official
title, because "Seller's Disclosure of Real Property Condition Report" is the
name of the form and a form_id would be strictly worse.

These call the real route. An earlier version of this file reimplemented the
naming rule and therefore passed against a deliberately broken route — the
mutation check is what caught it.
"""

import asyncio

import pytest

from state_compliance import routes_reference as rr


class _Ctx:
    """Minimal stand-in — the naming path never reads tenant state."""
    tenant_id = "00000000-0000-0000-0000-000000000000"
    role = "admin"


@pytest.fixture
def no_tenant_templates(monkeypatch):
    """No tenant-managed templates: isolates the seed-rule naming path.

    The executable-template branch appends separately and is not what these
    tests are about; stubbing it also keeps the DB out of a pure naming test.
    """
    async def _rows(ctx, code):
        return {}, [], [], []

    monkeypatch.setattr(rr, "_state_library_rows", _rows)


def _documents(state: str) -> list[dict]:
    payload = asyncio.run(rr.compliance_document_prelist(state, ctx=_Ctx()))
    return payload["required_documents"]


@pytest.mark.parametrize("state", ["DE", "CA", "TX", "NY", "FL"])
def test_no_two_required_documents_share_a_display_name(state, no_tenant_templates):
    names = [d["name"] for d in _documents(state)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"{state} renders indistinguishable checklist rows: {dupes}"


def test_respa_separates_the_loan_estimate_from_the_closing_disclosure(no_tenant_templates):
    by_form = {d.get("form_id"): d["name"] for d in _documents("DE") if d.get("form_id")}
    estimate = by_form.get("CFPB-LOAN-ESTIMATE")
    closing = by_form.get("CFPB-CLOSING-DISCLOSURE")
    assert estimate and closing, by_form
    assert estimate != closing


def test_lead_paint_disclosure_is_distinct_from_the_epa_pamphlet(no_tenant_templates):
    by_form = {d.get("form_id"): d["name"] for d in _documents("DE") if d.get("form_id")}
    assert by_form.get("EPA-HUD-LEAD-DISCLOSURE") != by_form.get("EPA-PROTECT-YOUR-FAMILY")


def test_a_single_document_rule_keeps_its_official_title(no_tenant_templates):
    # The regression guard: disambiguating must not degrade a real form name
    # into a database key.
    by_form = {d.get("form_id"): d["name"] for d in _documents("DE") if d.get("form_id")}
    assert by_form.get("DE-DISCLOSURE-REPORT") == (
        "Seller's Disclosure of Real Property Condition Report"
    ), by_form.get("DE-DISCLOSURE-REPORT")


def test_every_document_carries_the_obligation_it_satisfies(no_tenant_templates):
    # Losing the rule title would trade one ambiguity for another: the agent
    # needs to know which statutory obligation a derived name belongs to.
    for doc in _documents("DE"):
        assert doc.get("obligation"), doc


def test_derived_name_expands_the_identifier_without_inventing_one():
    assert rr._name_from_form_id("CFPB-LOAN-ESTIMATE") == "CFPB Loan Estimate"
    assert rr._name_from_form_id("EPA-PROTECT-YOUR-FAMILY") == "EPA Protect Your Family"
    assert rr._name_from_form_id("") == ""

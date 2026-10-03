"""scripts/audit-neoh-production.py — classification of a UI sweep.

The browser part needs a running Neoh; the verdict logic does not, and it is
what decides whether a launch audit says PASS. It must audit the CURRENT shell
(Home / Work / Neoh), never the retired tab names.
"""

from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_s = importlib.util.spec_from_file_location("audit_neoh_production", REPO / "scripts" / "audit-neoh-production.py")
a = importlib.util.module_from_spec(_s)
_s.loader.exec_module(a)


def _clean():
    return {
        "destinations": {n: {"alerts": []} for n in a.DESTINATIONS},
        "work_views": {n: {"alerts": []} for n in a.WORK_VIEWS},
        "admin": {"skipped": "this account is not a platform admin"},
        "websockets": [{"path": "/ws", "frames_received": 3}],
        "overflow": {"Home": False},
    }


def test_audits_the_current_shell_not_retired_tabs():
    assert a.DESTINATIONS == ("Home", "Work", "Neoh")
    for retired in ("Pipeline", "People", "Inbox", "Our AI", "Today"):
        assert retired not in a.DESTINATIONS
    src = (REPO / "scripts" / "audit-neoh-production.py").read_text(encoding="utf-8")
    assert "azurecontainerapps" not in src  # no default to a dead environment


def test_a_clean_sweep_passes():
    checks = a.classify(_clean())
    assert a.verdict(checks) == a.PASS, [c for c in checks if c["status"] != a.PASS]
    assert a.exit_code(checks) == 0


def test_failed_login_blocks_and_stops():
    checks = a.classify({"login_error": "the Home tab never appeared"})
    assert [c["id"] for c in checks] == ["login"]
    assert a.exit_code(checks) == 2


def test_a_missing_destination_or_5xx_or_no_realtime_blocks():
    r = _clean()
    del r["destinations"]["Neoh"]
    assert a.verdict(a.classify(r)) == a.BLOCKED
    r = _clean()
    r["failed_responses"] = [{"status": 502, "path": "/api/status", "method": "GET"}]
    assert a.verdict(a.classify(r)) == a.BLOCKED
    r = _clean()
    r["websockets"] = []
    assert a.verdict(a.classify(r)) == a.BLOCKED


def test_cosmetic_problems_only_warn():
    r = _clean()
    r["overflow"] = {"Work/Deals": True}
    r["stale_identities"] = ["JARVIS"]
    r["console_errors"] = ["x"]
    r["failed_responses"] = [{"status": 404, "path": "/api/x", "method": "GET"}]
    checks = a.classify(r)
    assert a.verdict(checks) == a.WARN

#!/usr/bin/env python3
"""Browser smoke test for the Oracle app Personal AI navigation surface."""

from __future__ import annotations

import argparse
import json

from playwright.sync_api import Route, sync_playwright


TENANT_ID = "11111111-1111-1111-1111-111111111111"


def api_fixture(url: str) -> dict:
    if "/billing/status/" in url:
        return {"active": True, "status": "active", "plan": "pro", "current_period_end": None}
    if "/api/contracts/documents" in url:
        return {"documents": [{
            "id": "document-1", "document_type": "assignment", "template_key": "assignment-agreement",
            "template_version": "1.0", "status": "approved", "created_at": "2026-07-15T12:00:00Z",
        }]}
    if "/api/contracts/templates" in url:
        return {"templates": [{
            "id": "template-1", "template_key": "assignment-agreement", "document_type": "assignment",
            "jurisdiction": "FL", "version": "1.0", "status": "approved",
        }]}
    if "/api/agents/me/onboarding" in url:
        return {
            "user_role": "agent",
            "membership": {"status": "approved", "training_status": "validated"},
            "licenses": [{"verification_status": "verified"}],
            "ai_settings": {
                "approved_tone": "concise",
                "autonomous_research": True,
                "autonomous_drafting": True,
                "style_training_opt_in": True,
            },
            "google_connected": True,
            "style_training_examples": 12,
        }
    if "/api/models" in url:
        return {"models": [{"id": "model-1", "name": "Agent Style", "version": "1.0", "model_kind": "agent_style_lora", "status": "active"}]}
    if "/api/commands/providers" in url:
        return {"providers": [{"id": "provider-1", "provider": "google", "disabled_at": None}]}
    if "/api/commands" in url:
        return {"commands": [{"id": "command-1", "command_type": "EMAIL", "state": "awaiting_approval", "created_at": "2026-07-15T12:00:00Z"}]}
    return {}


def fulfill_api(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(api_fixture(route.request.url)),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4173")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for label, viewport in {
            "mobile": {"width": 390, "height": 844},
            "desktop": {"width": 1024, "height": 900},
        }.items():
            context = browser.new_context(base_url=args.base_url, viewport=viewport)
            context.add_init_script(
                f"""
                sessionStorage.setItem('oracle_token', 'test-token');
                sessionStorage.setItem('oracle_role', 'agent');
                sessionStorage.setItem('oracle_onboarding_dismissed', '1');
                sessionStorage.removeItem('oracle_crm_tab');
                localStorage.setItem('oracle_user_id', 'test-agent');
                localStorage.setItem('oracle_tenant_id', '{TENANT_ID}');
                """
            )
            page = context.new_page()
            page.route("http://localhost:8000/**", fulfill_api)
            page.goto("/", wait_until="domcontentloaded")

            tab = page.get_by_role("tab", name="Personal AI")
            tab.wait_for(state="visible")
            tab.click()
            assert tab.get_attribute("aria-selected") == "true", f"{label}: tab did not activate"
            panel = page.get_by_role("tabpanel", name="Personal AI")
            panel.get_by_role("heading", name="Personal AI", exact=True).wait_for(state="visible")
            panel.get_by_text("Agent Style", exact=True).wait_for(state="visible")
            panel.get_by_text("Awaiting review", exact=True).wait_for(state="visible")
            capability_map = panel.get_by_text("Platform capability map", exact=True)
            capability_map.click()
            assert panel.locator("details ol > li").count() == 30, f"{label}: capability map is incomplete"

            tab.press("ArrowRight")
            contracts_tab = page.get_by_role("tab", name="Contracts")
            assert contracts_tab.evaluate("element => document.activeElement === element"), (
                f"{label}: ArrowRight did not move focus to Contracts"
            )
            assert tab.get_attribute("aria-selected") == "true", (
                f"{label}: ArrowRight unexpectedly changed the active view"
            )
            contracts_tab.press("Enter")
            assert contracts_tab.get_attribute("aria-selected") == "true", f"{label}: Enter did not activate Contracts"
            contract_panel = page.get_by_role("tabpanel", name="Contracts")
            contract_panel.get_by_role("heading", name="Contracts", exact=True).wait_for(state="visible")
            contract_panel.get_by_text("All contracts & documents", exact=True).wait_for(state="visible")
            contract_panel.get_by_text("Assignment", exact=True).wait_for(state="visible")
            contracts_tab.press("ArrowLeft")
            assert tab.evaluate("element => document.activeElement === element"), (
                f"{label}: ArrowLeft did not return focus to Personal AI"
            )
            assert contracts_tab.get_attribute("aria-selected") == "true", (
                f"{label}: ArrowLeft unexpectedly changed the active view"
            )
            tab.press("Enter")
            assert tab.get_attribute("aria-selected") == "true", f"{label}: Enter did not reactivate Personal AI"

            dimensions = page.evaluate(
                "() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth })"
            )
            assert dimensions["width"] <= dimensions["viewport"] + 2, (
                f"{label}: horizontal overflow {dimensions}"
            )
            context.close()
        browser.close()
    print("Personal AI tab browser checks passed for mobile and desktop.")


if __name__ == "__main__":
    main()

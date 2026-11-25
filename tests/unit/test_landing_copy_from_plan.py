"""First-look copy comes from the approved plan, never the raw first chat sentence."""

import asyncio
import json

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.firstlook import service  # noqa: E402
from app.firstlook.copy import fallback_copy, third_person  # noqa: E402

RAW = "I want to start a company that helps independent bookkeepers collect missing receipts from clients"


def test_first_person_lead_in_is_rewritten():
    out = third_person(RAW, "ReceiptPilot")
    assert out.startswith("ReceiptPilot helps independent bookkeepers")
    assert "want to start" not in out


def test_fallback_headline_and_job_are_never_raw_text():
    copy = fallback_copy(RAW, name="ReceiptPilot")
    assert copy["headline"].startswith("ReceiptPilot helps")
    assert "I want to start a company" not in json.dumps(copy)


def test_third_person_text_is_untouched():
    text = "A SaaS that helps solo bookkeepers collect receipts."
    assert third_person(text) == text


@pytest.mark.parametrize("name,summary,audience", [
    ("ReceiptPilot", "A service that helps solo bookkeepers collect receipts.", "solo bookkeepers"),
    ("ClayClub", "A community that helps families learn pottery together.", "families learning pottery"),
])
def test_plan_name_beats_venture_name_and_raw_idea(monkeypatch, name, summary, audience):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    seen = {}
    monkeypatch.setattr(service.versions_mod, "validate_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(service.versions_mod, "get_latest", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.firstlook.brand_assets.custom_logo_url", lambda *args: None)
    monkeypatch.setattr(service, "plan_brief", lambda v: {
        "name": name,
        "text": summary,
        "customer": audience,
    })

    async def forbidden_copy(*args, **kwargs):
        raise AssertionError("Local pages must be built by your connected coding tool")

    def prepare(workspace_id, venture_id, brief, **kwargs):
        seen.update({field: brief[field] for field in ("idea", "name", "audience")})
        return {"id": "page_order", "state": "approved"}

    monkeypatch.setattr(service, "generate_copy", forbidden_copy)
    monkeypatch.setattr(service, "prepare_local_page", prepare)
    result = asyncio.run(service.generate_first_look(
        "ws_x", "I want to start a company", venture_id="ven_x", venture_name="Old name"))
    assert result == {"id": "page_order", "state": "approved"}
    assert seen == {"idea": summary, "name": name, "audience": audience}


def test_product_name_prefers_stored_field_then_task_titles():
    stored = {"milestones": [{"tasks": [{"title": "Build for Other, a SaaS",
                                         "landing_expectations": {"product_name": "ReceiptPilot"}}]}]}
    assert service.plan_product_name(stored) == "ReceiptPilot"
    titled = {"milestones": [{"tasks": [{"title": "Landing page", "landing_expectations": {"product_name": ""},
                                         "description": "Build a static landing page for ReceiptPilot, a SaaS that helps"}]}]}
    assert service.plan_product_name(titled) == "ReceiptPilot"
    assert service.plan_product_name({"milestones": []}) == ""


def test_plan_summary_is_never_copy():
    import inspect

    assert "plan.summary" not in inspect.getsource(service.plan_brief)


def test_model_step_labels_are_not_numbers():
    from app.firstlook.copy import _normalize
    copy = _normalize({"headline": "H", "how_it_works": [{"step": "Upload client list", "text": "Upload client list"}]}, "idea")
    assert copy["how_it_works"] == [{"step": 1, "text": "Upload client list"}]

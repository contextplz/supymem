"""Supymem's clarification picks cannot establish founder traction."""
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/assumed.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.intake import smart
    from app.ventures import stage
    return smart, stage


@pytest.mark.parametrize("text,expected", [
    ("Assumed: We have 5-10 paying customers", False),
    ("  Assumed: We have 140 paying subscribers", False),
    ("Booking software\nAssumed: We have 140 paying subscribers", False),
    ("Booking software\nClarification: Assumed: We have 140 paying subscribers", False),
    ("Assumed: We have 140 paying subscribers\nI have no customers yet", False),
    ("Assumed: We have 140 paying subscribers\nWe have 3 paying customers", True),
    ("We have 140 paying subscribers", True),
])
def test_customer_detection_ignores_assumed_lines(modules, text, expected):
    smart, _ = modules
    assert smart._has_customers(text) is expected


@pytest.mark.parametrize("language", ["en", "hi", None])
def test_assumed_intake_cannot_promote_stage_or_existing_customers(modules, language):
    _, stage = modules
    answer = "Assumed: We have 5-10 paying customers"
    extracted = {"idea": "Booking software", "audience": answer, "stage_evidence": answer,
                 "language": language, "has_customers": True, "stage": "customers"}
    intake = SimpleNamespace(body_md=json.dumps({"extracted": extracted}),
                             kind="intake_decision", venture_id="ven_assumed")
    venture = SimpleNamespace(raw_idea=answer)
    session = SimpleNamespace(get=lambda _model, key: intake if key.startswith("intake_decision:") else venture)
    assert not stage.has_existing_customers(session, "ven_assumed")
    assert stage.derive_stage({"extracted": extracted}) == "idea"
    assert stage.derive_stage({"extracted": extracted, "signups": 1}) == "traction"
    assert extracted["has_customers"] is True


def test_assumed_founder_assets_do_not_prove_progress(modules):
    _, stage = modules
    assert not stage._present_answer("Assumed: I built the prototype")
    assert not stage._present_answer("Assumed: 10 customer contacts")


def test_intake_classification_cannot_extract_customers_from_an_assumption(modules):
    smart, _ = modules
    result = smart.classify_fallback("Booking software for bakeries\nAssumed: We have 5-10 paying customers")
    assert result.extracted.has_customers is not True
    assert result.extracted.stage != "customers"


def test_stage_helpers_drop_assumed_lines_before_customer_checks(modules):
    _, stage = modules
    assert stage._founder_words("Booking software\nClarification: Assumed: We have 140 paying subscribers") == "Booking software"
    assert stage.derive_stage({"extracted": {"idea": "Assumed: We have 140 paying subscribers", "has_customers": True,
                                             "stage": "customers"}}) != stage.FounderStage.TRACTION.value

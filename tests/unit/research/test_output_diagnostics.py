"""Invalid model output remains diagnosable without exposing its contents."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def diagnostics(tmp_path_factory):
    with pytest.MonkeyPatch.context() as env:
        env.setenv("COMPANY_OS_DATA_DIR", str(tmp_path_factory.mktemp("output-diagnostics")))
        env.setenv("LLM_OFFLINE", "1")
        load_company_os_app()
        from app.research.metered import _output_diagnostics
        yield _output_diagnostics


def test_diagnostics_distinguish_truncation_without_provider_text(diagnostics):
    content = '{"private_customer_data":"never log me"'
    try:
        json.loads(content)
    except ValueError as exc:
        result = diagnostics(content, SimpleNamespace(finish_reason="length"), exc)
    assert result == {"content_chars": len(content), "sha256": hashlib.sha256(content.encode()).hexdigest(),
                      "finish_reason": "length", "validation_kind": "json_syntax"}
    assert "private_customer" not in json.dumps(result)


def test_provider_error_and_finish_reason_cannot_enter_diagnostics(diagnostics):
    result = diagnostics("private", SimpleNamespace(finish_reason="private provider detail"), ValueError("private validation input"))
    assert result["finish_reason"] == "unknown" and result["validation_kind"] == "domain"
    assert "private" not in json.dumps(result)


def test_policy_diagnostic_only_accepts_fixed_rule_codes(diagnostics):
    from app.idea.validation_agent import ValidationPolicyError
    result = diagnostics("private", SimpleNamespace(finish_reason="stop"), ValidationPolicyError("unknown_citation", "private input"))
    assert result["validation_rule"] == "unknown_citation" and "private" not in json.dumps(result)
    unknown = diagnostics("private", None, ValidationPolicyError("private", "private"))
    assert "validation_rule" not in unknown and "private" not in json.dumps(unknown)


def test_transport_diagnostics_retain_timeout_cause_without_credentials(diagnostics):
    import httpx
    from openai import APITimeoutError
    from app.research.metered import _failure_diagnostics
    from llm_gateway.errors import classify_provider_error
    underlying = APITimeoutError(request=httpx.Request("POST", "https://private.example.test/?key=secret"))
    outer = classify_provider_error(underlying)
    outer.__cause__ = underlying
    result = _failure_diagnostics(outer, 120.125)
    assert result == {"category": "timeout", "elapsed_ms": 120125, "provider_code": "provider_down"}
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


def test_transport_diagnostics_status_and_codes_are_allowlisted(diagnostics):
    from app.research.metered import _failure_diagnostics
    from llm_gateway.errors import ProviderCallError
    result = _failure_diagnostics(ProviderCallError("private-code", "secret body", status=503), 2)
    assert result == {"category": "http", "elapsed_ms": 2000, "http_status": 503}
    assert _failure_diagnostics(ValueError("secret body"), -1) == {"category": "internal", "elapsed_ms": 0}


def test_boundary_failure_is_distinct_and_diagnostics_keep_only_ledger_reference(diagnostics):
    from app.research.metered import _failure, _failure_diagnostics
    from llm_gateway.boundary import BoundaryViolation
    error = BoundaryViolation("private content", reason="private content", ledger_id="bnd_safe_reference")
    assert _failure(error) == ("failed", "research_boundary_blocked")
    result = _failure_diagnostics(error, 0.285)
    assert result == {"category": "boundary", "elapsed_ms": 285, "boundary_ledger_id": "bnd_safe_reference"}
    assert "private" not in json.dumps(result)
    assert "boundary_ledger_id" not in _failure_diagnostics(BoundaryViolation("private", ledger_id="unsafe reference"), 0)


def test_research_domain_diagnostics_identify_evidence_failure_without_quotes(diagnostics):
    result = diagnostics("secret provider prose", None,
        ValueError("research quote is not present in its cited source excerpt"))
    assert result["validation_rule"] == "quote_mismatch"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(("message", "rule"), [
    ("research observation selections must be unique", "duplicate_observation_selection"),
    ("research quote index is outside provided excerpts", "quote_index_out_of_range"),
    ("research omission cannot fit the coverage gap limit", "omission_gap_limit"),
    ("research provider output exceeds limit or is invalid", "output_size_or_type"),
])
def test_research_selection_diagnostics_use_only_fixed_codes(diagnostics, message, rule):
    result = diagnostics("private model output", None, ValueError(message))
    assert result["validation_kind"] == "domain" and result["validation_rule"] == rule
    assert "validation_rule" not in diagnostics("private", None, ValueError(message + ": private"))
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("rule", ["content_topic_count", "content_unknown_evidence", "content_unknown_segment",
                                 "content_duplicate_angle", "content_visual_unsupported", "content_output_too_large"])
def test_content_domain_diagnostics_retain_only_fixed_reason(diagnostics, rule):
    result = diagnostics("unpublished private content", None, ValueError(rule))
    assert result["validation_rule"] == rule
    assert "private" not in json.dumps(result)
    assert "validation_rule" not in diagnostics("", None, ValueError(rule + ": private input"))


@pytest.mark.parametrize("rule", [
    "duplicate_milestone_key", "first_paid_user_journey_required", "landing_first_required",
    "invalid_milestone_dependencies", "included_milestone_requires_excluded_dependency",
    "duplicate_task_key", "unknown_capability", "foreign_research_source", "foreign_resource",
    "reuse_evidence_required", "landing_expectations_task_mismatch",
    "landing_expectations_incomplete", "landing_concept_current_claim", "founder_action_capability_mismatch", "founder_handoff_required",
    "native_product_source_acceptance_invalid", "native_product_source_contract_invalid",
    "native_product_source_coverage_invalid", "native_product_source_scope_invalid",
])
def test_plan_domain_diagnostics_retain_only_fixed_reason(diagnostics, rule):
    result = diagnostics("unpublished private plan", None, ValueError(rule))
    assert result["validation_rule"] == rule
    assert "private" not in json.dumps(result)
    assert "validation_rule" not in diagnostics("", None, ValueError(rule + ": private input"))


def test_journey_diagnostics_distinguish_structural_failure_without_prose(diagnostics):
    from app.planning.author import GENERATED_JOURNEY
    rows = [{"kind": kind, "included": True, "title": "secret customer name"} for kind in GENERATED_JOURNEY]
    rows[2]["included"] = False
    rows[1], rows[2] = rows[2], rows[1]
    result = diagnostics(json.dumps({"milestones": rows}), None, ValueError("first_paid_user_journey_required"))
    assert result["journey_count"] == 9
    assert result["journey_not_included_count"] == 1
    assert result["journey_order_matches"] is False
    assert result["journey_missing"] == result["journey_duplicate_kinds"] == []
    assert "secret" not in json.dumps(result)


def test_journey_diagnostics_redact_unknown_labels_and_match_group_normalization(diagnostics):
    rows = [{"kind": "landing", "included": True}, {"kind": "landing", "included": True},
            {"kind": {"secret": "nested"}, "included": "secret"}, {"kind": "secret customer name"}]
    result = diagnostics(json.dumps({"milestones": [rows]}), None, ValueError("first_paid_user_journey_required"))
    assert result["journey_kinds"] == ["landing", "landing", "unknown", "unknown"]
    assert result["journey_duplicate_kinds"] == ["landing"]
    assert result["journey_not_included_count"] == 2
    assert "secret" not in json.dumps(result)


def test_journey_diagnostics_are_bounded_and_never_change_rejection(diagnostics):
    raw = json.dumps({"milestones": [{"kind": "landing", "included": True}] * 1200})
    result = diagnostics(raw, None, ValueError("first_paid_user_journey_required"))
    assert result["journey_count"] == 1000
    assert len(result["journey_kinds"]) == 12
    assert diagnostics("{" * 121000, None, ValueError("first_paid_user_journey_required"))["journey_shape"] == "unavailable"


def test_named_journey_diagnostics_do_not_log_unknown_stage_names(diagnostics):
    raw = json.dumps({"journey": {"landing": {"included": False}, "secret customer name": {}}, "milestones": []})
    result = diagnostics(raw, None, ValueError("first_paid_user_journey_required"))
    assert result["journey_shape"] == "named_stages"
    assert result["journey_unknown_stage_count"] == 1
    assert result["journey_invalid_body_count"] == 1
    assert result["journey_conflicting_array"] is True
    assert "core" in result["journey_missing"]
    assert "secret" not in json.dumps(result)


def test_schema_paths_are_useful_without_rejected_values_or_unknown_field_names(diagnostics):
    from pydantic import BaseModel, ConfigDict, ValidationError
    class Item(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        title: str
    class Envelope(BaseModel):
        items: list[Item]
    with pytest.raises(ValidationError) as raised:
        Envelope.model_validate({"items": [{"title": 42, "secret_extra_field": "private customer input"}, {}]})
    result = diagnostics("private rejected prose", None, raised.value, schema=Envelope.model_json_schema())
    assert result["schema_issue_count"] == 3
    assert result["schema_issues"] == [
        {"path": ["items", 0, "title"], "code": "string_type"},
        {"path": ["items", 0, "<field>"], "code": "extra_forbidden"},
        {"path": ["items", 1, "title"], "code": "missing"},
    ]
    output = json.dumps(result)
    assert "private" not in output and "secret_extra_field" not in output and "42" not in json.dumps(result["schema_issues"])


def test_schema_issue_output_is_bounded(diagnostics):
    from pydantic import BaseModel, ValidationError
    class Envelope(BaseModel):
        items: list[int]
    with pytest.raises(ValidationError) as raised:
        Envelope.model_validate({"items": ["private"] * 30})
    result = diagnostics("private", None, raised.value, schema=Envelope.model_json_schema())
    assert result["schema_issue_count"] == 30 and len(result["schema_issues"]) == 12
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("private_error", ["target_id_invalid: private input", "private input", "unknown_machine_code"])
def test_custom_schema_diagnostic_never_serializes_unknown_context(diagnostics, private_error):
    from pydantic import BaseModel, ValidationError, model_validator
    class Item(BaseModel):
        value: str
        @model_validator(mode="after")
        def reject(self):
            raise ValueError(private_error)
    with pytest.raises(ValidationError) as raised:
        Item.model_validate({"value": "private customer input"})
    result = diagnostics("private model output", None, raised.value, schema=Item.model_json_schema())
    assert result["schema_issues"] == [{"path": [], "code": "value_error"}]
    assert "private" not in json.dumps(result) and "ctx" not in json.dumps(result)

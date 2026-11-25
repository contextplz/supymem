"""Preserved-context diagnostic uses real planner/schema and boundary logic."""
from copy import deepcopy
import json

import pytest

from tests.unit.planning.test_author import author as author, context as context


def test_preserved_request_matches_compiler_and_does_not_truncate_or_mutate(author, context):
    from app.planning import compiler, diagnostics, request_policy
    context["founder_instruction"] = "Retain café orders and founder-reviewed pricing."
    context["sources"].append({"id": "second_owned_source", "summary": "Observed Unicode café evidence " * 50})
    document = {"_runtime": {"context": deepcopy(context)}}
    before = deepcopy(document)
    request, projected, meta = diagnostics.prepared(document)
    expected, source_map, resource_map = compiler._project(context)
    assert request.messages == author.messages(expected) == request_policy.prepare(expected).messages
    assert json.loads(request.messages[-1]["content"]) == projected == expected
    assert document == before and meta["context_sha256"] == diagnostics.digest(context)
    assert meta["source_count"] == len(source_map) == 2 and meta["resource_count"] == len(resource_map) == 1
    assert meta["max_output_tokens"] == 16384
    assert meta["policy"]["provider_timeout_seconds"] == request.provider_timeout_seconds == 240
    context["sources"][1]["summary"] += " Meaningful new observed evidence."
    _, _, changed = diagnostics.prepared({"_runtime": {"context": context}})
    assert changed["context_sha256"] != meta["context_sha256"] and changed["request_sha256"] != meta["request_sha256"]


def test_diagnostic_audit_sink_preserves_real_secret_refusal(author, capsys):
    from app.boundary import Segment
    from app.boundary.filter import inspect_payload
    from llm_gateway.boundary import BoundaryViolation
    from app.planning.diagnostics import diagnostic_boundary_log
    private = "diagnostic-private-fixture-value"
    with diagnostic_boundary_log() as records, pytest.raises(BoundaryViolation):
        inspect_payload([{"role": "user", "content": private}], purpose="plan.compile", workspace_id=None,
            provider="foundry:DeepSeek-V4-Pro", segments=[Segment(text=private, declared_class="secret")])
    assert records and records[0]["blocked"] is True
    text = capsys.readouterr().out
    assert "operator_model_boundary" in text and private not in text


def test_request_compaction_preserves_all_evidence_and_schema(author, context):
    context["founder_instruction"] = 'Keep  two spaces, café, newlines\n and "quoted" requirements.'
    before = deepcopy(context)
    messages = author.messages(context)
    contract_text = messages[0]["content"][len(author.SYSTEM):]
    assert json.loads(contract_text) == author.generation_schema(context)
    assert json.loads(messages[1]["content"]) == before == context
    previous_bytes = len((json.dumps(author.schema(context)) + json.dumps(context, ensure_ascii=False)).encode())
    current_bytes = len((contract_text + messages[1]["content"]).encode())
    assert current_bytes < previous_bytes


def test_compaction_cannot_bypass_existing_context_admission_limit(author, context):
    # Many short fields have enough formatting overhead to cross the old bound
    # while compact JSON remains below it. Admission must not silently expand.
    context["extra_evidence"] = {str(i): "x" for i in range(15000)}
    assert len(json.dumps(context, ensure_ascii=False)) > 180_000
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) < 180_000
    with pytest.raises(ValueError, match="plan_context_too_large"):
        author.messages(context)


def test_plan_compile_has_its_own_bound_while_interactive_calls_keep_90():
    from app.planning import request_policy
    from app.research import metered
    assert request_policy.PROVIDER_TIMEOUT_SECONDS == 240 and metered.INTERACTIVE_TIMEOUT_SECONDS == 90


def test_plan_repair_note_names_the_exact_rejected_fields_from_production_diagnostics():
    """2026-10-06 Supplify plan.compile: three paid attempts failed on the same fields because the
    repair re-ask only said "keep strings within maxLength"; the note now names each field."""
    from app.research.metered import _schema_issue_note
    note = _schema_issue_note({"validation_kind": "schema", "schema_issues": [
        {"path": [0, "landing_evidence", "visuals", 1, "name"], "code": "string_too_long"},
        {"path": ["precedent_applications", 1, "proposed_application"], "code": "missing"},
        {"path": ["precedent_applications", 1, "<field>"], "code": "extra_forbidden"}]})
    assert "0.landing_evidence.visuals.1.name: shorten" in note
    assert "precedent_applications.1.proposed_application: this required field is missing" in note
    assert "precedent_applications.1.<field>: remove this field" in note
    assert _schema_issue_note({}) == "" and _schema_issue_note({"schema_issues": "x"}) == ""

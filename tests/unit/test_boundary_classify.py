"""Data-boundary classifier (T10.9–T10.13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "boundary"


@pytest.fixture()
def boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/b.db")
    load_company_os_app()
    from app.boundary.classify import classify
    from app.boundary import DataClass, Segment

    return classify, DataClass, Segment


def test_seven_classes_exist(boundary) -> None:
    _, DataClass, _ = boundary
    assert {c.value for c in DataClass} == {
        "public",
        "workspace_meta",
        "workspace_code",
        "workspace_business",
        "third_party_pii",
        "secret",
        "unknown",
    }


def test_stripe_key_is_secret(boundary) -> None:
    classify, DataClass, Segment = boundary
    result = classify([Segment(text="sk_test_" + ("a" * 24))])
    assert any(s.data_class is DataClass.SECRET for s in result.segments)
    assert not result.sendable


def test_email_is_pii(boundary) -> None:
    classify, DataClass, Segment = boundary
    result = classify([Segment(text="Contact jane.doe@gmail.com about the waitlist.")])
    assert any(f.data_class is DataClass.THIRD_PARTY_PII for f in result.findings)


def test_outreach_allows_only_the_published_recipient_email(boundary):
    from app.boundary.filter import inspect_payload
    from app.planning.diagnostics import diagnostic_boundary_log

    _, _, Segment = boundary
    email = "ada@analytical.com"
    text = f"Write outreach to Ada at Analytical. Published email: {email}."
    messages = [{"role": "user", "content": text}]
    segment = Segment(text=text, origin="published_outreach_recipient", published_contact_email=email)
    with diagnostic_boundary_log():
        allowed = inspect_payload(messages, purpose="gtm.draft_post", workspace_id=None,
            provider="test", segments=[segment])
        unrelated = inspect_payload(messages, purpose="research.deep", workspace_id=None,
            provider="test", segments=[segment])
    assert allowed.messages == messages
    assert email not in unrelated.messages[0]["content"]
    others = text + " Mention another person: bob@differentcompany.com."
    with diagnostic_boundary_log():
        redacted = inspect_payload([{"role": "user", "content": others}], purpose="gtm.draft_post",
            workspace_id=None, provider="test", segments=[Segment(text=others,
                origin="published_outreach_recipient", published_contact_email=email)])
        mismatched = inspect_payload(messages, purpose="gtm.draft_post", workspace_id=None,
            provider="test", segments=[Segment(text="different bytes",
                origin="published_outreach_recipient", published_contact_email=email)])
    assert email in redacted.messages[0]["content"]
    assert "bob@differentcompany.com" not in redacted.messages[0]["content"]
    assert email not in mismatched.messages[0]["content"]


@pytest.mark.parametrize("private", ["sk_live_" + "a" * 24,
    " ".join(f"person{index}@othercompany.com" for index in range(6)),
    "+14155551234 123-45-6789 +442071234567 +61212345678 444-55-6666"])
def test_outreach_recipient_allowance_never_exempts_other_private_data(boundary, private):
    from app.boundary.filter import inspect_payload
    from llm_gateway.boundary import BoundaryViolation
    from app.planning.diagnostics import diagnostic_boundary_log

    _, _, Segment = boundary
    email = "ada@analytical.com"
    text = f"Write outreach to Ada. Email: {email}. Other data: {private}"
    with diagnostic_boundary_log(), pytest.raises(BoundaryViolation):
        inspect_payload([{"role": "user", "content": text}], purpose="gtm.draft_post", workspace_id=None,
            provider="test", segments=[Segment(text=text, origin="published_outreach_recipient",
                published_contact_email=email)])


def test_near_miss_password_word_is_not_enough(boundary) -> None:
    classify, DataClass, Segment = boundary
    result = classify([Segment(text="Please reset the password on the demo account.")])
    assert result.sendable


def test_positive_corpus_all_refused(boundary) -> None:
    classify, _, Segment = boundary
    rows = json.loads((FIXTURES / "positives.json").read_text())
    missed = []
    for row in rows:
        result = classify([Segment(text=row["text"], path=row.get("path"))])
        if result.sendable:
            missed.append(row["id"])
    assert missed == []


def test_near_miss_corpus_stays_sendable(boundary) -> None:
    classify, _, Segment = boundary
    rows = json.loads((FIXTURES / "near_miss.json").read_text())
    false_pos = []
    for row in rows:
        result = classify([Segment(text=row["text"], path=row.get("path"))])
        if not result.sendable:
            false_pos.append(row["id"])
    assert len(false_pos) / max(len(rows), 1) < 0.05


def test_reviewed_build_tool_table_is_code_but_generic_table_is_refused(boundary) -> None:
    classify, DataClass, Segment = boundary
    table = "\n".join(f"file-{index}.html\t{index}" for index in range(30))

    generic = classify([Segment(text=table, label="tool output")])
    reviewed = classify([Segment(text=table, label="tool output",
        origin="reviewed_build_tool", declared_class=DataClass.WORKSPACE_CODE)])

    assert not generic.sendable
    assert reviewed.sendable
    assert reviewed.segments[0].data_class is DataClass.WORKSPACE_CODE


def test_reviewed_build_tool_provenance_never_overrides_a_secret(boundary) -> None:
    classify, DataClass, Segment = boundary
    table = "\n".join(f"row-{index}\tsk_live_{'a' * 24}" for index in range(30))

    result = classify([Segment(text=table, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE)])

    assert not result.sendable
    assert result.segments[0].data_class is DataClass.SECRET


def test_reviewed_build_tool_allows_code_call_assignments_but_not_literal_secrets(boundary) -> None:
    classify, DataClass, Segment = boundary
    code = (
        "auth = ensure_user_id(user_id)\n"
        "password = hashlib.sha256(payload)\n"
        "auth_token = request.headers.get("
    )
    reviewed = classify([Segment(text=code, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE)])
    generic = classify([Segment(text=code, label="untrusted output")])

    assert reviewed.sendable
    assert reviewed.segments[0].data_class is DataClass.WORKSPACE_CODE
    assert not generic.sendable

    literal = "auth_token = abcdefghijklmnopqrstuvwxyz123456"
    refused = classify([Segment(text=literal, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE)])
    assert not refused.sendable
    assert refused.segments[0].data_class is DataClass.SECRET


def test_reviewed_build_tool_does_not_exempt_call_shaped_literal_arguments(boundary) -> None:
    classify, DataClass, Segment = boundary
    literal = "auth_token = lookup(abcdefghijklmnopqrstuvwxyz123456)"
    result = classify([Segment(text=literal, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE)])

    assert not result.sendable
    assert result.segments[0].data_class is DataClass.SECRET


def test_post_redaction_scan_preserves_exact_reviewed_build_provenance(boundary) -> None:
    from app.boundary.filter import inspect_payload
    from app.planning.diagnostics import diagnostic_boundary_log

    _, DataClass, Segment = boundary
    terminal_output = "\x1b[32mPASS\x1b[0m\n" * 40
    messages = [{"role": "tool", "content": terminal_output, "tool_call_id": "call-1"}]
    segments = [Segment(text=terminal_output, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE, label="the tool build message")]

    with diagnostic_boundary_log() as records:
        decision = inspect_payload(messages, purpose="build.model", workspace_id=None,
            provider="foundry:DeepSeek-V4-Pro", segments=segments)

    assert decision.messages == messages
    assert records and records[0]["blocked"] is False


def test_post_redaction_scan_still_blocks_secret_in_reviewed_build_output(boundary) -> None:
    from app.boundary.filter import inspect_payload
    from llm_gateway.boundary import BoundaryViolation
    from app.planning.diagnostics import diagnostic_boundary_log

    _, DataClass, Segment = boundary
    private = "sk_live_" + ("a" * 24)
    messages = [{"role": "tool", "content": private, "tool_call_id": "call-1"}]
    segments = [Segment(text=private, origin="reviewed_build_tool",
        declared_class=DataClass.WORKSPACE_CODE, label="the tool build message")]

    with diagnostic_boundary_log() as records, pytest.raises(BoundaryViolation):
        inspect_payload(messages, purpose="build.model", workspace_id=None,
            provider="foundry:DeepSeek-V4-Pro", segments=segments)

    assert records and records[0]["blocked"] is True


@pytest.mark.asyncio
async def test_transmit_never_calls_provider_on_secret(boundary, monkeypatch) -> None:
    from llm_gateway.boundary import BoundaryViolation
    from llm_gateway.sole import transmit

    called = {"n": 0}

    async def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("provider must not be called")

    monkeypatch.setattr("llm_gateway.sole._call_provider", boom, raising=False)
    with pytest.raises(BoundaryViolation):
        await transmit(
            [{"role": "user", "content": "sk_live_" + ("b" * 24)}],
            purpose="research.deep",
            workspace_id="wsp_test",
        )
    assert called["n"] == 0

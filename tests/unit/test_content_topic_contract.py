"""The authored topic vocabulary must match the immutable brief parser."""
import json
import pytest
from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def topic_input(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.content import generation_schema as schema
    snapshot = {"brief": {"product": {"name": "Booking request concept"},
        "icp": {"segments": [{"name": "One independent rehearsal studio owner"}]},
        "claims": [], "competitors": []}, "count": 2, "avoid": ["Already reviewed"]}
    topic = {"angle": "Review incoming requests", "hook": "What needs approval?", "rationale": "Explore the concept",
             "icp_segment": "One independent rehearsal studio owner", "intent": "value", "evidence": ["product"], "score": .5}
    return schema, snapshot, topic


def test_prompt_enumerates_exact_allowed_references_and_count(topic_input):
    schema, snapshot, topic = topic_input
    body = json.loads(schema.messages("topics", snapshot)[1]["content"])
    fields = body["schema"]["$defs"]["Topic"]["properties"]
    assert fields["evidence"]["items"]["enum"] == ["product", "icp.segments.0"]
    assert fields["icp_segment"]["enum"] == [topic["icp_segment"]]
    assert body["schema"]["properties"]["topics"]["maxItems"] == 2
    assert schema.parse("topics", json.dumps({"topics": [topic]}), snapshot)["topics"] == [topic]
    other = {**snapshot, "count": 1, "brief": {**snapshot["brief"], "icp": {"segments": [{"name": "Different owner"}]}}}
    assert schema.topic_schema(other)["$defs"]["Topic"]["properties"]["icp_segment"]["enum"] == ["Different owner"]
    assert schema.topic_schema(snapshot)["$defs"]["Topic"]["properties"]["icp_segment"]["enum"] == [topic["icp_segment"]]


@pytest.mark.parametrize("field,value,reason", [("evidence", ["outside.source"], "content_unknown_evidence"),
    ("icp_segment", "Studio owners", "content_unknown_segment"), ("angle", "Already reviewed", "content_duplicate_angle")])
def test_foreign_or_duplicate_topic_is_rejected_without_coercion(topic_input, field, value, reason):
    schema, snapshot, topic = topic_input
    with pytest.raises(ValueError, match=reason):
        schema.parse("topics", json.dumps({"topics": [{**topic, field: value}]}), snapshot)


def test_topic_count_is_a_limit_not_a_fabricated_output_requirement(topic_input):
    schema, snapshot, topic = topic_input
    assert len(schema.parse("topics", json.dumps({"topics": [topic]}), snapshot)["topics"]) == 1
    with pytest.raises(ValueError, match="content_topic_count"):
        schema.parse("topics", json.dumps({"topics": [{**topic, "angle": str(i)} for i in range(3)]}), snapshot)


def test_reddit_topics_cannot_plan_an_unpublishable_promotion(topic_input):
    schema, snapshot, topic = topic_input
    scoped = {**snapshot, 'platform': 'reddit'}
    assert schema.topic_schema(scoped)['$defs']['Topic']['properties']['intent']['enum'] == ['value']
    with pytest.raises(ValueError, match='content_topic_intent_invalid'):
        schema.parse('topics', json.dumps({'topics': [{**topic, 'intent': 'promo'}]}), scoped)
    assert schema.parse('topics', json.dumps({'topics': [topic]}), scoped)['topics'] == [topic]
    # Historical channel-neutral topic receipts retain their admitted contract.
    assert schema.parse('topics', json.dumps({'topics': [{**topic, 'intent': 'promo'}]}), snapshot)


def test_reddit_writer_and_critic_share_honest_founder_role(topic_input):
    schema, snapshot, topic = topic_input
    scoped = {**snapshot, 'platform': 'reddit', 'topic': topic}
    writer = json.loads(schema.messages('writer', scoped)[1]['content'])
    critic = json.loads(schema.messages('critic', scoped, draft={'body': 'A discussion', 'title': 'Question'})[1]['content'])
    contract = writer['editorial_contract']
    assert contract == critic['editorial_contract']
    assert 'never impersonate a customer' in contract['platform_guidance']
    assert 'unvalidated' in contract['platform_guidance']
    assert '900 characters or more' not in contract['platform_guidance']

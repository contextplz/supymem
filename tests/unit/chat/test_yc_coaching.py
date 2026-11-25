"""Quote relevance, durable venture frequency and chat-save idempotency."""
import json
from types import SimpleNamespace
import pytest

from tests.company_os_harness import company_os_test_client, load_company_os_app
from tests.company_os_harness import configure_local_test_model
from tests.unit.chat.test_site_read_and_posting import chat_draft, chat_request, signed_calls, DRAFT_DOMAINS


@pytest.mark.parametrize("domain", DRAFT_DOMAINS)
def test_post_draft_keeps_displayed_and_saved_posting_text_exact(chat_draft, monkeypatch, domain):
    from app.chat import ground
    from app.org.characters import parse_speaker

    calls = []

    def quote(*args, **kwargs):
        calls.append(args)
        return {"id": "quote", "text": "Ask what you need to learn before choosing your next step.",
            "title": "Learning", "url": "https://example.invalid/quote"}

    monkeypatch.setattr(ground, "yc_quote_for_reply", quote)
    reply = chat_draft["write"](domain, status_context=False)
    body = parse_speaker(reply["body_md"])[1]
    assert calls == [] and body == chat_draft["draft"]
    (action,) = [action for action in reply["actions"] if action.get("confirmation")]
    assert action["confirmation"]["resulting_state"]["content"] == body


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state
    state.reset_db_factory()
    with company_os_test_client(main.create_app()) as result:
        yield result


@pytest.mark.parametrize("question", [
    "How should I price enterprise SaaS with a paid pilot and test willingness to pay?",
    "How can I improve consumer marketplace photos and listings conversion?",
])
def test_local_advice_prompt_uses_bundled_coaching_and_names_missing_files(client, monkeypatch, tmp_path, question):
    from app.chat import assistant
    from app.knowledge import pmf, yc_video_advice

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("YC_VIDEO_ADVICE_ENABLED", "0")
    brief = {"_rendered": "Your idea is a proposal, not customer evidence.",
             "idea": {"raw_idea": question}, "next_step": {"key": "idea.clarify"}}
    body = assistant.build_messages(question=question, brief=brief, history=[])[-1]["content"]
    assert "Founder coaching references" in body and '"sources"' in body and "https://" in body
    for module in (pmf, yc_video_advice):
        monkeypatch.setattr(module, "CORPUS", tmp_path / module.CORPUS.name)
        module.corpus.cache_clear()
        module.index.cache_clear()
    try:
        body = assistant.build_messages(question=question, brief=brief, history=[])[-1]["content"]
        assert all(module.CORPUS.name in body for module in (pmf, yc_video_advice))
        assert str(tmp_path / "knowledge") in body and "restart your app" in body
    finally:
        for module in (pmf, yc_video_advice):
            module.corpus.cache_clear()
            module.index.cache_clear()


@pytest.mark.parametrize("question", ["enterprise SaaS pricing paid pilot", "consumer marketplace photos listings conversion"])
@pytest.mark.parametrize("failure", ["missing", "damaged"])
def test_local_explicit_workbook_chat_request_names_the_restore_step(client, monkeypatch, tmp_path, question, failure):
    from app.chat.ground import _workbook_reference
    from app.knowledge import pmf

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(pmf, "CORPUS", tmp_path / pmf.CORPUS.name)
    pmf.corpus.cache_clear()
    pmf.index.cache_clear()
    if failure == "damaged":
        pmf.CORPUS.write_bytes(b"damaged coaching file")
    try:
        body = _workbook_reference(f"Show a workbook case study for {question}.")
        assert isinstance(body, str) and pmf.CORPUS.name in body
        assert str(tmp_path / "knowledge") in body and "restart your app" in body
    finally:
        pmf.corpus.cache_clear()
        pmf.index.cache_clear()


def test_relevance_and_frequency(client):
    from app.chat.ground import yc_quote_for_reply
    query = "willingness to pay pricing paid pilot enterprise sales"
    quote = [{"source": "yc_video_quote"}]
    assert yc_quote_for_reply("unrelated quantum neutrino crystals", []) is None
    assert yc_quote_for_reply(query, []) is not None
    for recent in ([quote], [[], quote], [[], [], quote]):
        assert yc_quote_for_reply(query, recent) is None
    assert yc_quote_for_reply(query, [[], [], [], quote]) is not None


@pytest.mark.parametrize("audience", [
    "inventory planners at independent pharmacies",
    "amateur birdwatchers with limited mobility",
])
def test_discovery_acknowledgements_abstain_from_coaching(client, monkeypatch, audience):
    from app.chat.ground import yc_quote_for_reply
    from app.knowledge import yc_video_advice

    def forbidden(*args, **kwargs):
        raise AssertionError("A discovery acknowledgement must not retrieve coaching")

    monkeypatch.setattr(yc_video_advice, "quote", forbidden)
    for request in (f"Find {audience} for customer interviews. Do not contact anyone.",
                    f"Find customers among {audience}."):
        assert yc_quote_for_reply(request, [], reply="I'll look for matching people. Nothing is sent.") is None


@pytest.mark.parametrize("domain", ["staff scheduling for dental practices", "running lessons for first-time parents"])
def test_requested_writing_does_not_start_unasked_reply_additions(client, monkeypatch, domain):
    from app.chat.ground import yc_quote_for_reply
    from app.chat.diagram_reply import wants_diagram
    from app.knowledge import yc_video_advice

    calls = []

    def quote(*args, **kwargs):
        calls.append(args)
        return {"text": "Unrequested coaching"}

    monkeypatch.setattr(yc_video_advice, "quote", quote)
    monkeypatch.setattr("app.core.local_mode.is_offline", lambda: False)
    monkeypatch.delenv("CHAT_DIAGRAM_STEP", raising=False)
    question = f"Write a pricing interview plan and script for {domain}."
    body = "Your interview questions cover the most recent problem, existing workaround and willingness to try your proposed offer. " * 5
    assert (yc_quote_for_reply(question, [], reply=body) is None, not wants_diagram(question, body)) == (True, True)
    assert calls == []
    assert wants_diagram(question + " Draw a diagram of the interview steps.", body)


def test_saved_chat_quotes_unprompted_only_on_topic_and_throttles_per_venture(client, monkeypatch):
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.knowledge import yc_video_advice
    from app.org.characters import stamp_speaker
    from app.org.manager import manager_speaker
    monkeypatch.setattr("app.ventures.idea_page.request_refresh", lambda *args, **kwargs: True)
    vid = client.post("/ventures", json={"raw_idea": "A pricing pilot for bakeries", "name": "Pilot"}).json()["id"]
    other = client.post("/ventures", json={"raw_idea": "A pricing pilot for families planning trips" if is_local() else "Another pricing pilot", "name": "Other"}).json()["id"]
    if is_local():
        monkeypatch.setattr(yc_video_advice, "search", lambda *args, **kwargs: [{"id": "pricing-reference",
            "topics": ["pricing", "willingness to pay", "paid pilot"], "action_discussed": "Discuss willingness to pay, pricing and paid pilot commitments",
            "source_video": {"title": "Pricing interviews", "url": "https://www.youtube.com/watch?v=real"},
            "evidence": {"excerpts": ["Discuss pricing and your pilot with customers who are willing to pay."]}}])
    append_message(vid, role="user", body_md="unrelated quantum neutrino crystals")
    assert "YC says:" not in append_message(vid, role="assistant", body_md="I have no saved evidence.")["body_md"]
    append_message(vid, role="user", body_md="willingness to pay pricing paid pilot enterprise sales")
    body = stamp_speaker("Test willingness to pay with pricing conversations.", manager_speaker(vid))
    first = append_message(vid, role="assistant", body_md=body, idempotency_key="coaching-first")
    assert 'YC says: “' in first["body_md"] and "youtube.com/watch?v=" not in first["body_md"]
    assert any(c.get("source") == "yc_video_quote" for c in first["citations"])
    assert len([c for c in first["citations"] if "youtube.com/watch?v=" in c.get("href", "")]) == 1
    retry = append_message(vid, role="assistant", body_md=body, idempotency_key="coaching-first")
    assert retry["id"] == first["id"] and retry["body_md"] == first["body_md"]
    for _ in range(3):
        assert "YC says:" not in append_message(vid, role="assistant", body_md="Review customer commitments.")["body_md"]
    assert "YC says:" in append_message(vid, role="assistant", body_md="Discuss pricing and willingness to pay with customers.")["body_md"]
    append_message(other, role="user", body_md="willingness to pay pricing paid pilot enterprise sales")
    assert "YC says:" in append_message(other, role="assistant", body_md="Discuss pricing and willingness to pay with customers.")["body_md"]
    with pytest.raises(ValueError, match="chat_idempotency_conflict"):
        append_message(vid, role="assistant", body_md="A different reply.", idempotency_key="coaching-first")


def test_employee_and_exact_evidence_replies_are_not_modified(client):
    from app.chat.store import append_message
    vid = client.post("/ventures", json={"raw_idea": "Pricing pilot", "name": "Pilot"}).json()["id"]
    append_message(vid, role="user", body_md="willingness to pay pricing paid pilot enterprise sales")
    assert "YC says:" not in append_message(vid, role="assistant", body_md="<!--employee:Maya:Research:researcher-->\nSaved findings.")["body_md"]
    exact = append_message(vid, role="assistant", body_md="Exact saved evidence.", preserve_exact=True)
    assert "YC says:" not in exact["body_md"]


def test_u12_baby_sleep_review_abstains_from_ai_building_quote(client, monkeypatch):
    from app.chat.store import append_message
    from app.knowledge import yc_video_advice as yc
    assert "Quad Code" not in json.dumps(yc.corpus())
    row = {"id": "boris", "title": "Building Claude Code", "topics": ["AI model capabilities", "product development strategy"],
        "source_video": {"title": "Building Claude Code", "url": "https://www.youtube.com/watch?v=eCjYIj-fEDw"},
        "evidence": {"excerpts": ["When I first created Claude Code I did not know if I was onto something."]}}
    monkeypatch.setattr(yc, "search", lambda *args, **kwargs: [row])
    vid = client.post("/ventures", json={"raw_idea": "App for parents tracking reading assignments; free + $6/mo", "name": "PupilLog QA"}).json()["id"]
    append_message(vid, role="user", body_md="When I first created this product I did not know if I was onto something")
    result = append_message(vid, role="assistant", body_md="Review the reading log audience. Interview parents about how they track school reading.")
    assert "YC says:" not in result["body_md"] and not result.get("citations")


def test_u12_legacy_inline_quote_does_not_repeat_source_in_footer(client):
    from app.chat.store import _ser
    from core.clock import utcnow
    url = "https://www.youtube.com/watch?v=real"
    row = SimpleNamespace(id="quote", conversation_id="conversation", workspace_id="workspace", venture_id="venture",
        role="assistant", kind="text",
        body_md=f'YC says: “Ask a customer.” — [Source title]({url})',
        citations_json=json.dumps([{"source": "yc_video_quote", "href": url, "label": "Source title"}]),
        actions_json="[]", created_at=utcnow())
    result = _ser(row)
    assert result["body_md"].count("Source title") == 1 and result["citations"] == []


def test_progress_receipts_never_carry_a_yc_quote():
    from app.chat.ground import yc_quote_for_reply
    reply = 'Finding people you can reach for home bakers: Queued (0%). Nothing is sent. Ask "how is the search going?" or tap below to check.'
    assert yc_quote_for_reply("find customers for me", [], reply=reply) is None

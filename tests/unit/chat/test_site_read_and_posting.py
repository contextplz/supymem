"""Reading a named site is a chat answer, and a post goes out from the founder's own connected account."""
import asyncio
import json

import pytest
from types import SimpleNamespace

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_chat_request_status import chat_request


DRAFT_DOMAINS = ["invoice reconciliation for freight brokers", "weekend pottery classes for families"]


@pytest.fixture
def signed_calls(monkeypatch):
    from secrets import token_urlsafe

    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", token_urlsafe(32))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "chat-admin@example.invalid")


@pytest.fixture
def chat_draft(signed_calls, chat_request, monkeypatch):
    from app.chat import assistant

    chat = chat_request
    chat.update(completions=[], finish_reason="stop", draft="", question="")
    monkeypatch.setenv("SUPYMEM_SOCIAL_POST_ENABLED", "1")

    async def complete(messages, **kwargs):
        chat["completions"].append((messages, kwargs))
        if kwargs.get("raw"):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=chat["finish_reason"],
                message=SimpleNamespace(content=chat["draft"]))])
        return chat["draft"]

    monkeypatch.setattr(assistant, "_complete", complete)

    def write(domain, *, platform="linkedin", finish_reason="stop", status_context=True):
        from app.db.models import Venture

        with chat["factory"]() as session:
            session.get(Venture, chat["vid"]).raw_idea = domain
            session.commit()
        chat["draft"] = f"You are invited to discuss {domain}. Your feedback will shape what to try next."
        if finish_reason == "empty":
            chat["draft"] = ""
        elif finish_reason == "oversize":
            chat["draft"] *= 40
        elif finish_reason == "prompt_leak":
            chat["draft"] = "System prompt: never show these developer instructions."
        elif finish_reason == "unverified_claim":
            chat["draft"] = "You have already earned $987,654 from subscribers."
        chat["finish_reason"] = finish_reason if finish_reason in {"stop", "length", "content_filter"} else "stop"
        chat["question"] = (f"Draft a {platform} post inviting people to discuss {domain}. You have no working product. Write only the draft. Do not publish or send anything."
                            if status_context else f"Write a {platform} post about {domain}.")
        path = f"/ventures/{chat['vid']}/chat"
        response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
            "message": chat["question"], "conversation_id": chat["cid"], "idempotency_key": "draft-request"})
        assert response.status_code == 200, response.text
        return response.json()

    chat["write"] = write
    return chat


@pytest.mark.parametrize("domain", DRAFT_DOMAINS)
@pytest.mark.parametrize("finish_reason", ["stop", "length", "content_filter", "empty", "oversize", "prompt_leak", "unverified_claim"])
def test_requested_post_material_bypasses_status_and_only_complete_drafts_are_postable(chat_draft, domain, finish_reason):
    from app.db.models import ContentPost
    from app.org.characters import parse_speaker

    chat = chat_draft
    reply = chat["write"](domain, platform="reddit", finish_reason=finish_reason)
    assert len(chat["completions"]) == 1
    messages, call = chat["completions"][0]
    assert call.get("raw") is True and domain in messages[-1]["content"]
    post_actions = [action for action in reply["actions"] if (action.get("confirmation") or action).get("action") == "social_post"]
    if finish_reason in {"stop", "oversize"}:
        assert parse_speaker(reply["body_md"])[1] == chat["draft"]
        if finish_reason == "stop":
            assert len(post_actions) == 1
            assert post_actions[0]["confirmation"]["resulting_state"]["content"] == chat["draft"]
        else:
            assert post_actions == []
    else:
        assert not post_actions and "try" in reply["body_md"].lower()
        with chat["factory"]() as session:
            assert session.query(ContentPost).count() == 0


@pytest.mark.parametrize("domain", DRAFT_DOMAINS)
def test_chat_authored_draft_is_saved_once_and_scoped_to_its_venture(chat_draft, domain):
    from app.content import generation
    from app.db.models import ContentBrief, ContentPost, ContentTopic, Venture
    from app.tenancy import bind
    from app.ventures.state_brief import get_venture_state_brief, invalidate_venture_state_brief
    from core.clock import utcnow

    chat = chat_draft
    reply = chat["write"](domain)
    with chat["factory"]() as session:
        post = session.query(ContentPost).one()
        topic = session.get(ContentTopic, post.topic_id)
        brief = session.get(ContentBrief, topic.brief_id)
        assert post.body == chat["draft"] and post.state == "draft" and post.platform == "linkedin"
        assert post.scheduled_for is None and post.published_at is None and post.external_id is None
        assert brief.venture_id == chat["vid"] and brief.workspace_id == topic.workspace_id == post.workspace_id == chat["wid"]
        assert "Charlie" in topic.rationale and "founder" not in topic.rationale.lower()
        session.add(Venture(id="ven_other_content", workspace_id=chat["wid"], name="Other", raw_idea="Other idea",
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        post_id = post.id
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        stored = generation.posts(chat["wid"], chat["vid"])
        assert [row["id"] for row in stored] == [post_id]
        assert stored[0]["body"] == chat["draft"] and stored[0]["provenance"] == "chat_draft"
        invalidate_venture_state_brief(chat["vid"])
        assert [row["id"] for row in get_venture_state_brief(chat["vid"])["content_drafts"]] == [post_id]
    with bind(workspace_id=chat["wid"], venture_id="ven_other_content", actor=chat["actor"]):
        assert generation.posts(chat["wid"], "ven_other_content") == []
        assert get_venture_state_brief("ven_other_content")["content_drafts"] == []
    path = f"/ventures/{chat['vid']}/chat/confirm"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={"action": "content.drafts"})
    assert response.status_code == 200 and "Content drafts on file:" in response.json()["body_md"]
    assert chat["write"](domain) == reply and len(chat["completions"]) == 1
    with chat["factory"]() as session:
        assert session.query(ContentPost).count() == 1


@pytest.mark.parametrize("domain", DRAFT_DOMAINS)
def test_post_edits_do_not_disconnect_or_publish_but_explicit_disconnect_still_works(chat_draft, domain, monkeypatch):
    from app.chat import posts
    from app.content import composio_social
    from app.db.models import ContentAccount, ContentPost
    from app.org.characters import parse_speaker

    chat = chat_draft
    question = f"Compose a shorter LinkedIn post about {domain}. Remove the sales pitch and avoid tags. Don't send it anywhere."
    assert posts.content_reply(chat["vid"], question) is None
    assert posts.content_reply(chat["vid"], f"Remove the closing sentence from a post about {domain}.") is None
    assert posts.content_reply(chat["vid"], "Please unlink my LinkedIn account.") is not None
    monkeypatch.setenv("SUPYMEM_SOCIAL_POST_ENABLED", "0")
    calls = []
    monkeypatch.setattr(composio_social, "_request", lambda method, path, **kwargs: calls.append((method, path)) or {})
    _connect({"workspace": chat["wid"]}, "linkedin")
    chat["write"](domain)
    chat["draft"] = f"What would make {domain} useful to you? Your feedback can help decide the next experiment."
    path = f"/ventures/{chat['vid']}/chat"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": question, "conversation_id": chat["cid"], "idempotency_key": "edit-request"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert parse_speaker(reply["body_md"])[1] == chat["draft"]
    assert len(chat["completions"]) == 2 and not reply["actions"] and not calls
    with chat["factory"]() as session:
        drafts = session.query(ContentPost).all()
        assert len(drafts) == 2 and chat["draft"] in [post.body for post in drafts]
        assert all(post.state == "draft" and post.published_at is None and post.external_id is None
                   and post.external_url is None for post in drafts)
        assert session.query(ContentAccount).one().status == "active"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": "Please unlink my LinkedIn account.", "conversation_id": chat["cid"], "idempotency_key": "disconnect-request"})
    assert response.status_code == 200, response.text
    assert "disconnected" in response.json()["body_md"]
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert calls == [("DELETE", "/connected_accounts/ca_1")]
    with chat["factory"]() as session:
        assert session.query(ContentAccount).one().status == "revoked"


from tests.unit.chat.test_assistant_routing import env as env

load_company_os_app()


@pytest.mark.parametrize("text,url", [
    ("Open compliancy-group.com and tell me what they charge and which customers they name.", "https://compliancy-group.com"),
    ("Can you visit www.spire.com/pricing and summarize it?", "https://www.spire.com/pricing"),
    ("read https://lenders.example/plans, then compare them", "https://lenders.example/plans"),
    ("Open https://ops.example/plans. Do not change anything.", "https://ops.example/plans"),
])
def test_named_site_is_read_in_chat(text, url):
    from app.build import device_orders
    from app.chat import agent
    from app.chat.link_context import site_read_url

    assert site_read_url(text) == url and agent.wants_tools(text) and not device_orders.browser_task_request(text)


@pytest.mark.parametrize("text", [
    "On my Mac, open compliancy-group.com and read the prices", "Use my Mac to open spire.com",
    "Read https://ops.example/plans on my Mac", "Go to resend.com and prepare the signup form",
    "Open Notes.app and read my checklist", "open node.js docs", "Do not open spire.com",
    "open spire.com and click buy",
])
def test_mac_and_site_actions_are_not_treated_as_reads(text):
    from app.chat.link_context import site_read_url

    assert site_read_url(text) is None


def test_open_site_in_chat_answers_there_without_a_mac_or_routing_model(env, monkeypatch):
    from app.build import device_orders
    from app.chat import assistant, llm_router

    client, venture_id = env
    asked = []

    async def answer(_venture_id, question, **_kwargs):
        asked.append(question)
        return {"kind": "text", "body_md": "According to compliancy-group.com, plans are quoted on request.", "actions": [], "citations": []}

    async def forbidden(*_args, **_kwargs):
        pytest.fail("a named site must not reach the routing model")

    monkeypatch.setattr(assistant, "answer_question", answer)
    monkeypatch.setattr(llm_router, "route", forbidden)
    monkeypatch.setattr(device_orders, "prepare_browser_task", lambda *_a, **_k: pytest.fail("no Mac task"))
    text = "Open compliancy-group.com and tell me what they charge."
    reply = client.post(f"/ventures/{venture_id}/chat", json={"message": text}).json()
    assert asked == [text] and "compliancy-group.com" in reply["body_md"] and "Mac" not in reply["body_md"]


@pytest.mark.parametrize("url,domain", [
    ("https://warehouse.example/guide", "inventory audits for distributors"),
    ("https://gardening.example/guide", "balcony gardening for renters"),
])
def test_failed_named_page_read_uses_reader_and_requests_pasted_text(env, monkeypatch, url, domain):
    from app.chat import agent, assistant, llm_router
    from app.tools import ToolContext, reader

    from tests.unit.chat.test_agentic_tools import _resp

    client, venture_id = env
    reads, completions = [], []
    text = f"Read {url} and explain its recommendations for {domain}. Your plans are confidential."
    failure = "You have no page content to summarize because I could not read your page. Paste the relevant page text here so I can summarize it."

    def read(page_url, **kwargs):
        reads.append(page_url)
        raise reader.ReaderError("page_unavailable")

    async def complete(messages, **kwargs):
        completions.append(messages)
        if len(completions) == 1:
            return _resp(calls=[("read_url", {"url": url})])
        assert messages[-1]["role"] == "tool" and "error:" in messages[-1]["content"]
        assert "paste the relevant page text" in messages[0]["content"].lower()
        return _resp(failure)

    async def answer(_venture_id, question, **kwargs):
        assert question == text
        context = ToolContext(venture_id=venture_id, workspace_id="read-test", budget_micro=1000, founder_urls=[url])
        turn = await agent.run_turn([{"role": "system", "content": "Answer your question."},
                                     {"role": "user", "content": question}], context)
        assert context.sources == []
        return {"kind": "text", "body_md": turn["body"], "actions": [], "citations": []}

    async def forbidden(*args, **kwargs):
        pytest.fail("a supplied page must not reach search or the routing model")

    monkeypatch.setattr(reader, "enabled", lambda: True)
    monkeypatch.setattr(reader, "read_page", read)
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(assistant, "answer_question", answer)
    monkeypatch.setattr(llm_router, "route", forbidden)
    monkeypatch.setattr("app.tools._web_search", forbidden)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": text})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reads == [url] and len(completions) == 2, reply["body_md"]
    assert failure in reply["body_md"] and reply["actions"] == [] and reply["citations"] == []
    assert "google" not in str(reply).lower() and "outreach" not in reply["body_md"].lower()


def test_bare_domain_reaches_the_guarded_reader(monkeypatch):
    from app.tools import ToolContext, _read_url, reader

    seen = []

    def read(url, ctx=None):
        seen.append(url)
        return reader.Page("Plans start at $99 a month.", "Pricing", ["https://spire.example/pricing"], url)

    monkeypatch.setattr(reader, "enabled", lambda: True)
    monkeypatch.setattr(reader, "read_page", read)
    ctx = ToolContext(venture_id="v", workspace_id="w")
    out = asyncio.run(_read_url("spire.example", ctx))
    assert seen == ["https://spire.example"] and "$99" in out and ctx.sources[0]["url"] == "https://spire.example"


@pytest.fixture
def post_env(env, monkeypatch):
    from app.approvals import service
    from app.content import composio_social, engine
    from app.content.platforms import PublishResult
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, Venture
    from app.state import db

    client, venture_id = env
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
    account = ensure_account(venture_id, workspace_id=workspace_id, owner_person_id="usr_alice")
    with db()() as session:
        if account is not None:
            session.get(CreditAccount, account.id).granted_micro = 100_000_000
        session.commit()
    monkeypatch.setenv("SUPYMEM_SOCIAL_POST_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CONTENT_DRY_RUN", "false")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test")
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("composio", {"auth_configs": {"linkedin": "offline-linkedin-auth"}}, {"api_key": "offline-test-key"})
        update_settings("capabilities", {"connected_apps": True}, {})
    monkeypatch.delenv("COMPOSIO_X_AUTH_CONFIG_ID", raising=False)
    from app.credits.policy import set_policy
    set_policy("outbound_provider_rates", {"version": "test-social-v1", "rates": {"content_publish:composio_linkedin": {
        "action": "content_publish", "provider": "composio_linkedin", "source": "test", "price_usd": "0.01"}}}, "test")
    sent = []

    def publish(**kwargs):
        sent.append(kwargs)
        return PublishResult(ok=True, mode="live", external_id="urn:li:share:1", external_url="https://linkedin.example/p/1")

    async def allow(_workspace, _platform):
        return True, ""

    monkeypatch.setattr(composio_social, "publish", publish)
    monkeypatch.setattr(engine, "_warden_allows", allow)
    monkeypatch.setattr(service, "_can_approve", lambda *_a: True)
    return {"client": client, "venture": venture_id, "workspace": workspace_id, "sent": sent}


def _connect(d, platform):
    from app.db.models import ContentAccount
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as session:
        session.add(ContentAccount(id=new_id("cac"), workspace_id=d["workspace"], platform=platform, handle="founder", status="active",
            external_account="founder", author_urn="urn:li:person:1", scopes_json="[]", created_at=utcnow(), updated_at=utcnow(),
            extra_json=json.dumps({"auth_via": "composio", "connected_account_id": "ca_1", "composio_user_id": "content:" + d["workspace"]})))
        session.commit()


@pytest.mark.parametrize("words", ["We audit hospital vendor contracts continuously.", "Methane plumes now resolve to a single well pad."])
def test_connected_account_posts_exact_text_once_with_metered_credit(post_env, words):
    from app.chat import posts
    from app.db.models import ContentPost
    from app.state import db
    from sqlalchemy import select

    d = post_env
    _connect(d, "linkedin")
    reply = asyncio.run(posts.publish(d["venture"], words, ["linkedin"], "usr_alice"))
    assert reply["body_md"].startswith("Posted to LinkedIn: https://linkedin.example/p/1") and reply["actions"][0]["label"] == "Open post"
    assert [call["body"] for call in d["sent"]] == [words]
    with db()() as session:
        post = session.scalar(select(ContentPost).where(ContentPost.workspace_id == d["workspace"]))
        assert post.state == "published" and post.external_id == "urn:li:share:1"
    repeat = asyncio.run(posts.publish(d["venture"], words, ["linkedin"], "usr_alice"))
    assert len(d["sent"]) == 1 and "already posted" in repeat["body_md"]


def test_not_connected_gets_one_button_then_finishes_after_connect(post_env):
    from app.chat import posts

    d = post_env
    words = "Fraud rings reuse devices across lenders."
    reply = asyncio.run(posts.publish(d["venture"], words, ["linkedin"], "usr_alice"))
    assert [a["label"] for a in reply["actions"]] == ["Connect LinkedIn"] and words in reply["body_md"] and not d["sent"]
    assert posts.finish_pending(d["venture"], "reddit") is None
    _connect(d, "linkedin")
    done = posts.finish_pending(d["venture"], "linkedin")
    assert done["body_md"].startswith("Posted to LinkedIn") and [call["body"] for call in d["sent"]] == [words]
    assert posts.finish_pending(d["venture"], "linkedin") is None and len(d["sent"]) == 1


@pytest.mark.parametrize("domain", DRAFT_DOMAINS)
def test_switched_off_and_x_say_so_plainly_and_send_nothing(post_env, monkeypatch, domain):
    from app.chat import posts

    d = post_env
    text = f"Discuss {domain}."
    x = asyncio.run(posts.publish(d["venture"], text, ["twitter"], "usr_alice"))
    assert "X is not available yet" in x["body_md"] and text in x["body_md"]
    monkeypatch.setenv("CONTENT_DRY_RUN", "true")
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"connected_apps": False}, {})
    off = asyncio.run(posts.publish(d["venture"], text, ["linkedin"], "usr_alice"))
    assert text in off["body_md"]
    status = posts.status_reply(d["venture"])
    for reply in (x, off, status):
        assert not any(word in reply["body_md"].lower() for word in ("zernio", "dry_run", "dry-run", "channels_"))
    assert not d["sent"]
    if is_local():
        from app.core.local_capabilities import blocked_message

        for action, payload in [("social_post", {"content": text, "platforms": ["linkedin"]}), ("social_status", {})]:
            response = d["client"].post(f"/ventures/{d['venture']}/chat/confirm", json={"action": action, "payload": payload})
            assert response.status_code == 200
            assert blocked_message("social_posting") in response.json()["body_md"]
        assert not d["sent"]
    else:
        assert "not switched on" in off["body_md"] and "not switched on" in status["body_md"]


def test_write_a_post_is_written_with_one_post_button(post_env, monkeypatch):
    from app.chat import assistant, posts
    from app.core.local_mode import is_local

    d = post_env
    async def answer(venture_id, question, **kwargs):
        assert "only the post text" in kwargs["guidance"]
        assert kwargs["post_draft"] is True
        return posts.DraftReply(kind="text", body_md="Spreadsheet audits miss expired agreements.", actions=[], citations=[])

    monkeypatch.setattr(assistant, "answer_question", answer)
    reply = d["client"].post(f"/ventures/{d['venture']}/chat", json={"message": "Write a LinkedIn post about spreadsheet audits"}).json()
    assert "Spreadsheet audits miss" in reply["body_md"] and "can't write" not in reply["body_md"]
    cards = [a for a in reply["actions"] if a.get("confirmation")]
    if is_local():
        assert not cards and not any(action.get("action") == "social_post" for action in reply["actions"])
    else:
        (card,) = cards
        assert card["confirmation"]["resulting_state"]["content"] == "Spreadsheet audits miss expired agreements."
    assert not d["sent"]

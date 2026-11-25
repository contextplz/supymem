"""Owner journeys through local HTTP, replayed model transport and the real worker."""

import asyncio
import io
import imaplib
import json
import random
import re
import socket
import smtplib
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from tests.unit.test_local_mode import _owner_client, local_owner_app


@pytest.fixture(params=[
    pytest.param({"idea": "A proposed purchase-order checker for industrial suppliers",
                  "audience": "Purchasing teams at industrial suppliers"}, id="b2b"),
    pytest.param({"idea": "A proposed trail journal for weekend walkers",
                  "audience": "Weekend walkers planning their next route"}, id="consumer"),
])
def scenario(request):
    return request.param


@pytest.fixture
def boundary(monkeypatch):
    """Replace external transports, never routes, handlers or capability decisions."""
    calls = []

    def record(method, url):
        host = urlsplit(str(url)).hostname or ""
        calls.append((method, host))
        assert host != "supymem.com" and not host.endswith(".supymem.com")

    def response(request):
        record(request.method, request.url)
        return httpx.Response(200, json={"results": [], "data": [], "items": []}, request=request)

    async def async_http(transport, request):
        return response(request)

    def sync_http(transport, request):
        return response(request)

    def urlopen(request, *args, **kwargs):
        record(getattr(request, "method", None) or "GET", getattr(request, "full_url", request))
        return io.BytesIO(b'{"results": [], "data": []}')

    def no_socket(address, *args, **kwargs):
        record("socket", "http://" + str(address[0]))
        raise AssertionError("External sockets must use the fake transport.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", sync_http)
    original_urlopen = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    for module in tuple(sys.modules.values()):
        if module and getattr(module, "urlopen", None) is original_urlopen:
            monkeypatch.setattr(module, "urlopen", urlopen)
    monkeypatch.setattr(socket, "create_connection", no_socket)

    class MailTransport:
        def __init__(self, host, *args, **kwargs):
            self.host = host
            record("mail_connect", "http://" + host)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, **kwargs):
            return 220, b"Ready"

        def login(self, *args):
            return "OK", [b"Authenticated"]

        def sendmail(self, sender, recipients, message, **kwargs):
            record("send_email", "http://" + self.host)
            return {}

        def select(self, *args, **kwargs):
            return "OK", [b"0"]

        def uid(self, *args):
            return "OK", [b""]

        def response(self, name):
            return "OK", [b"1"]

    monkeypatch.setattr(smtplib, "SMTP", MailTransport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", MailTransport)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", MailTransport)
    import requests

    def requests_send(session, request, **kwargs):
        record(request.method, request.url)
        result = requests.Response()
        result.status_code = 200
        result._content = b'{"results": [], "data": []}'
        result.request = request
        return result

    monkeypatch.setattr(requests.Session, "send", requests_send)
    return calls


class ContentFlow:
    def __init__(self, client, scenario, boundary, tmp_path):
        self.client = client
        self.scenario = scenario
        self.boundary = boundary
        self.tmp_path = tmp_path
        self.secrets = ["synthetic-content-model-key"]
        self.model_calls = []
        venture = self.request("POST", "/ventures", {"raw_idea": scenario["idea"], "start": False}, 201)
        self.venture_id = venture["id"]
        self.workspace_id = venture["workspace_id"]
        self.client.headers["X-Workspace-Id"] = self.workspace_id

    def request(self, method, path, body=None, status=200, **kwargs):
        response = self.client.request(method, path, **({"json": body} if body is not None else {}), **kwargs)
        assert not any(secret in response.text for secret in self.secrets), "An HTTP response exposed a saved key."
        assert response.status_code == status, response.text
        return response.json()

    def drain(self):
        from app.worker.main import run_worker

        async def once():
            await run_worker(once=True, stop=asyncio.Event())

        self.client.portal.call(once)

    def enqueue(self, kind, platform=None, key=None):
        request = {"venture_id": self.venture_id, "kind": kind, "brief_id": self.brief_id, "count": 1}
        if platform:
            request["platform"] = platform
        quote = self.request("POST", "/api/v1/content/generation/quote", request)
        envelope = {**request, "idempotency_key": key or "owner-request-" + kind,
                    **{field: quote[field] for field in ("input_digest", "resource_digest", "ceiling_micro")}}
        path = "/api/v1/content/" + ("topics/refill" if kind == "topics" else "draft")
        return self.request("POST", path, envelope, 202), path, envelope

    def generate(self, kind, platform=None):
        run, path, envelope = self.enqueue(kind, platform)
        repeated = self.request("POST", path, envelope, 202)
        assert repeated["run_id"] == run["run_id"]
        self.drain()
        completed = self.request("GET", "/api/v1/content/generation/runs/" + run["run_id"],
                                 params={"venture_id": self.venture_id})
        assert completed["state"] == "completed", completed
        return completed

    def posts(self):
        return self.request("GET", "/api/v1/content/posts", params={"venture_id": self.venture_id})["posts"]

    def draft(self, platform="linkedin"):
        self.generate("topics")
        self.generate("draft", platform)
        return self.posts()[0]

    def upload(self):
        from PIL import Image

        image = Image.frombytes("RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        result = self.request("POST", "/api/v1/marketing/assets", files={"file": ("product.png", stream.getvalue(), "image/png")},
                              data={"kind": "product", "title": self.scenario["idea"]})
        assert result["ok"], result
        return result["asset"]

    def assert_no_delivery(self):
        from sqlalchemy import select
        from app.db.models import GtmTouch
        from app.state import db

        assert self.boundary == [], "Drafting, reviewing or downloading must not contact an external service."
        with db()() as session:
            assert not session.scalar(select(GtmTouch.id).where(GtmTouch.sent_at.is_not(None)))
        assert all(post["published_at"] is None and post["external_id"] is None and post["scheduled_for"] is None
                   for post in self.posts())


@pytest.fixture
def flow(local_owner_app, monkeypatch, tmp_path, scenario, boundary):
    """Use the lock-test fresh-schema pattern; test startup migrations separately."""
    from llm_gateway import cassette, sole
    from llm_gateway.client import LLMClient
    from app.marketing.media import store
    from app.core import local_env_import
    from app.db import local_sqlite

    monkeypatch.setattr(local_sqlite, "migrate_sqlite", local_sqlite.ensure_sqlite_parent)
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setenv("CONTENT_AGENT_ENABLED", "false")
    monkeypatch.setattr(store, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(local_env_import, "repo_root", lambda: tmp_path)
    for name in local_env_import._FIELDS:
        monkeypatch.delenv(name, raising=False)
    from app.core.providers_local import PROVIDER_ENV

    for names in PROVIDER_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("IMAGEIO_NO_INTERNET", "1")
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        client.headers["Origin"] = "http://localhost:3100"
        journey = ContentFlow(client, scenario, boundary, tmp_path)
        journey.request("PUT", "/local/providers", {"provider": "openai", "api_key": journey.secrets[0],
                        "base_url": "https://model.example/v1", "models": {"chat": "gpt-4o-mini"}})
        journey.request("PUT", "/local/capabilities/content", {"on": True})
        journey.request("PUT", "/local/capabilities/send_email", {"on": False})
        journey.request("PUT", "/local/capabilities/connected_apps", {"on": False})
        journey.request("PUT", "/local/capabilities/publish_pages", {"on": False})
        brief = journey.request("POST", "/api/v1/content/brief", {"venture_id": journey.venture_id, "brief": {
            "product": {"name": "Your proposed project", "one_liner": scenario["idea"]},
            "icp": {"segments": [{"name": scenario["audience"], "pains": ["Keeping an accurate record"]}]},
            "claims": [], "competitors": [], "voice": {}, "cadence": {},
        }})
        journey.brief_id = brief["id"]
        journey.body = scenario["idea"] + ".\n\n" + (
            "This is a proposal, not a claim that the product is already available. "
            "Before you adopt a new tool, write down what you need to record, which details you check "
            "and where you keep the original information. Compare the proposed workflow with your current "
            "one. Keep the original record alongside each proposed change so you can check it later. "
            "Which part would you keep doing yourself, and which part would you want help with?")
        outputs = {
            "content.topics": {"topics": [{"angle": "Review your records", "hook": "What do you check?",
                "rationale": "Explore the proposed workflow", "icp_segment": scenario["audience"], "intent": "value",
                "evidence": ["product"], "score": .8}]},
            "content.writer": {"body": journey.body, "title": "", "visual": None},
            "content.critic": {"score": .9, "verdict": "publish", "problems": [], "unsupported_claims": [],
                "reads_as_ai": False, "claims_reviewed": True, "claim_checks": [{"draft_quote": scenario["idea"],
                    "status": "proposal", "evidence": [{"source_id": "product", "quote": scenario["idea"]}],
                    "reason": "The proposal preserves the stated product stage."}]},
        }
        for purpose, output in outputs.items():
            monkeypatch.setitem(cassette.DETERMINISTIC_FIXTURES, purpose,
                                {"content": json.dumps(output), "tokens_in": 100, "tokens_out": 180})
        monkeypatch.setitem(cassette.DETERMINISTIC_FIXTURES, "venture.chat.assistant",
                            {"content": journey.body, "tokens_in": 100, "tokens_out": 180})

        async def replay(messages=None, **kwargs):
            journey.model_calls.append(kwargs["purpose"])
            output = await LLMClient(config=kwargs["config"], offline=True).chat(messages, purpose=kwargs["purpose"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output.content), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=output.tokens_in, completion_tokens=output.tokens_out,
                                      total_tokens=output.tokens_in + output.tokens_out), model="gpt-4o-mini")

        monkeypatch.setattr(sole, "transmit", replay)
        journey.drain()
        journey.model_calls.clear()
        yield journey


def test_local_app_starts_with_real_sqlite_migrations(local_owner_app, boundary, scenario):
    """You can open your local app before trying content, without a missing Python import."""
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        response = client.post("/ventures", json={"raw_idea": scenario["idea"], "start": False})
        assert response.status_code == 201


@pytest.mark.parametrize("platform", ["linkedin", "devto"], ids=["post", "article"])
def test_topics_draft_review_edit_and_markdown_handoff(flow, platform):
    """Your requested posts and articles finish, are reviewable and never auto-publish."""
    post = flow.draft(platform)
    assert post["body"] == flow.body and post["state"] == "draft"
    assert post["critic"]["claims_reviewed"] is True
    assert post["critic"]["unsupported_claims"] == []
    assert post["handoff"]["text"] == flow.body
    markdown = post["handoff"]["markdown"]
    filename = post["handoff"]["filename"]
    assert Path(filename).name == filename and filename.endswith(".md")
    destination = flow.tmp_path / filename
    destination.write_text(markdown)
    assert destination.read_text() == flow.body + "\n"
    assert "Content" in post["handoff"]["next_step"] and "Copy" in post["handoff"]["next_step"]
    edited = flow.request("PUT", f"/api/v1/content/posts/{post['id']}/draft", {
        "venture_id": flow.venture_id, "body": flow.body + "\n\nWhat would you record first?",
        "title": "", "expected_digest": post["edit_digest"]})
    assert edited["state"] == "draft" and edited["model_review_required"] and not edited["scheduled"]
    refreshed = flow.posts()[0]
    assert not refreshed["critic"]["previous_model_review_applies"]
    assert refreshed["handoff"]["text"].endswith("What would you record first?")
    assert flow.model_calls == ["content.topics", "content.writer", "content.critic"]
    flow.assert_no_delivery()


@pytest.mark.parametrize("state", ["off", "needs_setup"])
@pytest.mark.parametrize("endpoint", ["generation/quote", "topics/refill", "draft"])
def test_content_blocked_states_name_exact_screen_and_setting(flow, state, endpoint):
    """Blocked work explains the owner-settable switch or missing provider key, before enqueue."""
    if state == "off":
        capability = flow.request("PUT", "/local/capabilities/content", {"on": False})
        expected = ("Settings > Capabilities", "Draft content", "switch on")
    else:
        from app.core.local_secrets import update_settings

        update_settings("model", {"provider": "anthropic", "providers": {"anthropic": {
            "base_url": "https://api.anthropic.com", "models": {"chat": "claude-sonnet-4-5"}}}}, {})
        capability = next(item for item in flow.request("GET", "/local/capabilities")["capabilities"] if item["name"] == "content")
        expected = ("Settings > Keys and connections > Your AI provider", "API key")
    assert capability["state"] == state
    body = {"venture_id": flow.venture_id, "kind": "draft" if endpoint == "draft" else "topics", "brief_id": flow.brief_id}
    if endpoint != "generation/quote":
        body.update(idempotency_key="blocked-owner-request", input_digest="0" * 64, resource_digest="0" * 64, ceiling_micro=0)
    reply = flow.request("POST", "/api/v1/content/" + endpoint, body, 409)
    message = reply["detail"]["message"]
    assert message == capability["next_step"]
    assert all(word in message for word in expected) and "\u2014" not in message
    flow.drain()
    assert not flow.model_calls and flow.posts() == []
    assert flow.request("GET", "/api/v1/content/generation/runs", params={"venture_id": flow.venture_id}) == {"runs": []}
    flow.assert_no_delivery()


def test_opening_content_and_reading_status_does_not_start_work(flow):
    """Your feature switch is permission, not a request to draft, send or publish."""
    assert flow.posts() == []
    flow.request("GET", "/api/v1/content/status", params={"venture_id": flow.venture_id})
    flow.request("GET", "/api/v1/content/agent")
    flow.drain()
    assert flow.model_calls == [] and flow.posts() == []
    flow.assert_no_delivery()


def test_replay_drafts_work_with_standard_offline_mode(flow, monkeypatch):
    """The shipped offline/replay mode can finish your requested content with no network."""
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    post = flow.draft()
    reviewed = flow.request("POST", f"/api/v1/content/posts/{post['id']}/review")
    assert reviewed["critic"]["claims_reviewed"] is True
    assert post["handoff"]["text"] == flow.body
    assert post["handoff"]["markdown"] == flow.body + "\n"
    assert post["handoff"]["filename"].endswith(".md")
    assert flow.model_calls == ["content.topics", "content.writer", "content.critic"]
    flow.assert_no_delivery()


def test_product_still_image_review_and_download(flow):
    """You can compose your pixels without a model key, review the image and download it."""
    from app.core.local_secrets import update_settings

    uploaded = flow.upload()
    update_settings("model", {"provider": "anthropic", "providers": {"anthropic": {
        "base_url": "https://api.anthropic.com", "models": {"chat": "claude-sonnet-4-5"}}}}, {})
    capability = flow.request("PUT", "/local/capabilities/content", {"on": True})
    assert capability["state"] == "needs_setup"
    result = flow.request("POST", "/api/v1/marketing/media/image", {"headline": flow.scenario["idea"], "width": 320, "height": 400})
    assert result["ok"], result
    asset = result["asset"]
    assert asset["mime"] in {"image/png", "image/jpeg"} and asset["id"] != uploaded["id"]
    reviewed = flow.request("POST", f"/api/v1/marketing/assets/{asset['id']}/review", {"status": "approved"})
    assert reviewed["asset"]["review_status"] == "approved"
    downloaded = flow.client.get(f"/api/v1/marketing/assets/{asset['id']}/file")
    assert downloaded.status_code == 200 and downloaded.content == Path(asset["path"]).read_bytes()
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    flow.assert_no_delivery()


def test_short_video_uses_reviewed_stills_and_local_encoder(flow):
    """Your requested 15-second video uses uploaded stills, not a generative video service."""
    flow.upload()
    image = flow.request("POST", "/api/v1/marketing/media/image", {"headline": flow.scenario["idea"], "width": 320, "height": 400})
    assert image["ok"], image
    flow.request("POST", f"/api/v1/marketing/assets/{image['asset']['id']}/review", {"status": "approved"})
    result = flow.request("POST", "/api/v1/marketing/media/video", {"headline": flow.scenario["idea"], "seconds": 15})
    assert result["ok"], result
    asset = result["asset"]
    assert asset["source"] == "ffmpeg" and asset["duration_ms"] == 15000
    assert asset["license"]["generative_video"] is False
    assert asset["meta"]["frames"] == [image["asset"]["path"]]
    downloaded = flow.client.get(f"/api/v1/marketing/assets/{asset['id']}/file")
    assert downloaded.status_code == 200 and downloaded.content == Path(asset["path"]).read_bytes()
    assert downloaded.headers["content-type"] == "video/mp4"
    flow.assert_no_delivery()


@pytest.mark.parametrize("kind", ["image", "video"])
def test_optional_media_respects_content_off_switch(flow, kind):
    """Switching off content stops optional media work before any asset or job is made."""
    from sqlalchemy import func, select
    from app.db.models import MarketingJob, MarketingPlan
    from app.state import db

    flow.upload()
    capability = flow.request("PUT", "/local/capabilities/content", {"on": False})
    before = flow.request("GET", "/api/v1/marketing/assets")["assets"]
    with db()() as session:
        jobs_before = session.scalar(select(func.count()).select_from(MarketingJob))
        plans_before = session.scalar(select(func.count()).select_from(MarketingPlan))
    result = flow.request("POST", "/api/v1/marketing/media/" + kind, {"headline": flow.scenario["idea"]})
    assert not result["ok"] and result["status"] == "blocked", result
    assert capability["next_step"] in json.dumps(result)
    assert flow.request("GET", "/api/v1/marketing/assets")["assets"] == before
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(MarketingJob)) == jobs_before
        assert session.scalar(select(func.count()).select_from(MarketingPlan)) == plans_before
    flow.assert_no_delivery()


def test_missing_image_input_names_upload_and_optional_key(flow):
    """A draft needs no image key; an image with no input tells you where to upload or save one."""
    result = flow.request("POST", "/api/v1/marketing/media/image", {"headline": flow.scenario["idea"]})
    assert not result["ok"]
    assert "Upload your product screenshot or photo in Media Studio" in result["block_reason"]
    assert "BFL image key in Keys and connections" in result["block_reason"]
    flow.assert_no_delivery()


def test_missing_encoder_names_install_command_and_retry_screen(flow, monkeypatch):
    """If ffmpeg is absent, you get the exact install command and Media Studio retry step."""
    from app.marketing.media import video

    monkeypatch.setattr(video, "ffmpeg_bin", lambda: None)
    result = flow.request("POST", "/api/v1/marketing/media/video", {"headline": flow.scenario["idea"]})
    assert not result["ok"] and result["status"] == "blocked"
    assert result["block_reason"] == video.ffmpeg_install_message()
    assert "Install ffmpeg" in result["block_reason"] and "Media Studio" in result["block_reason"]
    flow.assert_no_delivery()


def test_draft_is_reviewable_without_publishing_account_or_approval_card(flow):
    """Asking to review your draft must not demand a social account or create a publish approval."""
    post = flow.draft()
    result = flow.request("POST", f"/api/v1/content/posts/{post['id']}/review")
    assert result.get("critic", {}).get("claims_reviewed") is True
    assert not result.get("approval") and result.get("kind") != "confirmation"
    assert not re.search(r"\b\d+(?:\.\d+)?\s+credits?\b", json.dumps(result), re.I)
    flow.assert_no_delivery()


@pytest.mark.parametrize("state", ["off", "needs_setup"])
@pytest.mark.parametrize("entrypoint", ["engine", "http"])
def test_requested_social_publication_block_names_owner_next_step(flow, state, entrypoint):
    """An explicit publish request while off or unconfigured tells you the exact app setting."""
    post = flow.draft()
    capability = flow.request("PUT", "/local/capabilities/connected_apps", {"on": state != "off"})
    assert capability["state"] == state
    if entrypoint == "engine":
        from app.content.engine import execute_approved_post

        result = asyncio.run(execute_approved_post(flow.workspace_id, post["id"], approval_id=None))
        assert not result["ok"] and result["reason"] == "external_execution_disabled"
        message = result["message"]
    else:
        result = flow.request("POST", f"/api/v1/content/posts/{post['id']}/execute", {}, 409)
        message = result["detail"]["message"]
    assert message == capability["next_step"]
    flow.assert_no_delivery()


def test_saved_secrets_are_redacted_across_content_and_settings(flow):
    """Your saved provider, mail, search, publishing and app keys never come back in HTTP."""
    for section, fields in {
        "search": {"provider": "tavily", "api_key": "synthetic-search-key"},
        "smtp": {"host": "smtp.example", "port": 587, "user": "owner@example.invalid",
                 "password": "synthetic-mail-key", "sender_name": "Your team", "postal_address": "123 Test Road"},
        "publishing": {"provider": "vercel", "project": "test-project", "token": "synthetic-publish-key"},
        "composio": {"api_key": "synthetic-app-key", "auth_configs": {"linkedin": "test-auth-config"}},
    }.items():
        flow.secrets.extend(value for key, value in fields.items() if key in {"api_key", "password", "token"})
        flow.request("PUT", "/local/settings/" + section, fields)
        flow.request("GET", "/local/settings/" + section)
    flow.request("GET", "/local/settings/catalog")
    flow.request("GET", "/local/providers")
    for name in ("send_email", "publish_pages", "connected_apps"):
        assert flow.request("PUT", "/local/capabilities/" + name, {"on": True})["state"] == "ready"
    flow.draft()
    flow.request("GET", "/api/v1/content/status", params={"venture_id": flow.venture_id})
    flow.assert_no_delivery()
    for path in flow.tmp_path.rglob("*"):
        if path.is_file():
            assert not any(secret.encode() in path.read_bytes() for secret in flow.secrets), "A saved key was written in plaintext."


def test_requested_chat_draft_has_no_confirmation_card_or_credit_count(flow):
    """You get the post you asked for in chat, not a confirmation or credit-count message."""
    reply = flow.request("POST", f"/ventures/{flow.venture_id}/chat", {
        "message": f"Draft a LinkedIn post about {flow.scenario['idea']}. Write only the draft. Do not publish or send anything.",
        "idempotency_key": "requested-chat-draft"})
    flow.drain()
    assert reply["kind"] != "confirmation"
    assert flow.body in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply.get("actions", []))
    assert not re.search(r"\b\d+(?:\.\d+)?\s+credits?\b", json.dumps(reply), re.I)
    flow.assert_no_delivery()


def test_content_does_not_change_suppression_opt_out_or_contact_send_count(flow):
    """Preparing content never reactivates opted-out contacts or consumes their send allowance."""
    from core.clock import utcnow
    from sqlalchemy import select
    from app.db.models import GtmContact, GtmSuppression
    from app.state import db

    now = utcnow()
    status_path = f"/api/v1/ventures/{flow.venture_id}/gtm/journey"
    before = flow.request("GET", status_path)
    address = "opted-out@example.invalid"
    with db()() as session:
        session.add(GtmContact(id="opted-out-contact", workspace_id=flow.workspace_id, email=address,
            source=f"venture:{flow.venture_id}:public", enrichment_json=json.dumps({"found_at": "https://source.example/contact"}),
            do_not_contact=1, unsubscribed_at=now, times_emailed=4, stage_changed_at=now, created_at=now))
        session.add(GtmSuppression(id="content-suppression", workspace_id=flow.workspace_id, email=address,
                                   source="opt_out", reason="You asked not to be contacted", created_at=now))
        session.commit()
    flow.draft()
    after = flow.request("GET", status_path)
    assert (after["daily_cap"], after["sends_left_today"]) == (before["daily_cap"], before["sends_left_today"])
    with db()() as session:
        contact = session.get(GtmContact, "opted-out-contact")
        assert contact.do_not_contact == 1 and contact.unsubscribed_at.replace(tzinfo=now.tzinfo) == now
        assert contact.times_emailed == 4
        assert json.loads(contact.enrichment_json)["found_at"] == "https://source.example/contact"
        assert session.scalar(select(GtmSuppression.email)) == address
    flow.assert_no_delivery()


def test_email_draft_copy_preserves_source_disclosure_opt_out_and_address(flow):
    """Your email copy includes its source, opt-out and address; copying never sends it."""
    flow.request("PUT", "/local/settings/smtp", {
        "user": "owner@example.invalid", "sender_name": "Your project owner", "postal_address": "123 Test Road"})
    flow.request("PATCH", "/api/v1/outreach/onboarding", {
        "who": flow.scenario["audience"], "roles": [flow.scenario["audience"]], "sender_geo": "US",
        "sender_postal_address": "123 Test Road", "outbound_mode": "human_clipboard"})
    contact = flow.request("POST", "/api/v1/outreach/ingest", {
        "source": "profile_paste", "url": "https://source.example/contact",
        "raw_text": f"Jordan Example\ncontact@source.example\n{flow.scenario['audience']}\n\"Keeping an accurate record is difficult\"",
        "enrich": False})
    flow.request("PATCH", f"/api/v1/outreach/contacts/{contact['contact_id']}", {
        "why_this_week": "You shared the record-keeping problem this week", "recipient_geo": "US",
        "role": flow.scenario["audience"]})
    draft = flow.request("POST", "/api/v1/outreach/drafts", {"contact_id": contact["contact_id"], "kind": "email"})
    copied = flow.request("POST", f"/api/v1/outreach/drafts/{draft['id']}/copy", {})
    assert copied["copied_at"] and copied["sent_at"] is None
    assert "Your email address is listed publicly on source.example." in draft["body"]
    assert 'reply "no thanks"' in draft["body"].lower()
    assert "Reply STOP to stop these emails." in draft["body"]
    assert "Your project owner" in draft["body"].splitlines()
    assert "From: owner@example.invalid" in draft["body"]
    assert "123 Test Road" in draft["body"]
    flow.assert_no_delivery()


def test_person_image_requires_your_photo_and_names_upload_screen(flow):
    """Without your own photo, a person image stays blocked and explains the correct upload."""
    result = flow.request("POST", "/api/v1/marketing/media/image", {
        "headline": flow.scenario["idea"], "want_person": True, "allow_remote": True})
    assert not result["ok"] and result["status"] == "blocked"
    assert "founder photo" in result["block_reason"].lower() and "Media Studio" in result["block_reason"]
    assert "your own" in result["block_reason"] and "key" not in result["block_reason"].lower()
    assert flow.request("GET", "/api/v1/marketing/assets")["assets"] == []
    flow.assert_no_delivery()


@pytest.fixture
def failed_content(flow, monkeypatch):
    from llm_gateway import sole

    async def fail(messages=None, **kwargs):
        raise RuntimeError("Transport rejected " + flow.secrets[0])

    monkeypatch.setattr(sole, "transmit", fail)
    run, _, _ = flow.enqueue("topics")
    flow.drain()
    status = flow.request("GET", "/api/v1/content/generation/runs/" + run["run_id"],
                          params={"venture_id": flow.venture_id})
    return flow, status


def test_model_transport_failure_does_not_return_or_log_saved_secret(failed_content, caplog):
    """Even a provider error containing your key never returns it or writes it to logs."""
    flow, status = failed_content
    assert status["state"] in {"failed", "needs_input"}
    assert not any(secret in caplog.text for secret in flow.secrets)
    flow.assert_no_delivery()


def test_failed_content_run_names_exact_provider_setting_and_screen(failed_content):
    """A failed draft's saved progress names the provider setting and screen to fix."""
    flow, status = failed_content
    assert "Keys and connections" in status["next_step"]
    assert "provider" in status["next_step"].lower() and "key" in status["next_step"].lower()
    recent = flow.request("GET", "/api/v1/content/generation/runs", params={"venture_id": flow.venture_id})
    assert recent["runs"][0]["next_step"] == status["next_step"]
    from app.content.generation import Run
    from app.state import db

    with db()() as session:
        row = session.get(Run, status["run_id"])
        row.state, row.error_code = "needs_input", "content_outcome_uncertain"
        session.commit()
    uncertain = flow.request("GET", "/api/v1/content/generation/runs/" + status["run_id"],
                             params={"venture_id": flow.venture_id})
    assert uncertain["state"] == "needs_input"
    assert "provider's request history" in uncertain["next_step"]
    assert "do not repeat work" in uncertain["next_step"]
    assert "Then retry" not in uncertain["next_step"]
    for error in ("content_quality_refused", "content_near_duplicate"):
        with db()() as session:
            row = session.get(Run, status["run_id"])
            row.state, row.error_code = "refused", error
            session.commit()
        refused = flow.request("GET", "/api/v1/content/generation/runs/" + status["run_id"],
                               params={"venture_id": flow.venture_id})
        assert "draft's claim checks" in refused["next_step"]
        assert "edit your saved draft" in refused["next_step"]
        assert "key" not in refused["next_step"]
    flow.assert_no_delivery()


@pytest.mark.parametrize("state", ["off", "needs_setup"])
def test_chat_content_blocked_states_name_exact_next_step(flow, state):
    """Chat obeys the same Content switch and provider setup as the studio."""
    if state == "off":
        capability = flow.request("PUT", "/local/capabilities/content", {"on": False})
    else:
        from app.core.local_secrets import update_settings

        update_settings("model", {"provider": "anthropic", "providers": {"anthropic": {
            "base_url": "https://api.anthropic.com", "models": {"chat": "claude-sonnet-4-5"}}}}, {})
        capability = next(item for item in flow.request("GET", "/local/capabilities")["capabilities"] if item["name"] == "content")
    reply = flow.request("POST", f"/ventures/{flow.venture_id}/chat", {
        "message": f"Draft a LinkedIn post about {flow.scenario['idea']}. Do not publish it.",
        "idempotency_key": "blocked-chat-draft"})
    assert capability["next_step"] in reply["body_md"]
    assert reply["kind"] != "confirmation" and flow.model_calls == []
    flow.drain()
    assert flow.posts() == []
    flow.assert_no_delivery()

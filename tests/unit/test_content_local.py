import asyncio
import json
import platform as host_platform
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def local_content(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/content.db")
    load_company_os_app()
    from app.core import local_env_import, providers_local
    from app.state import reset_db_factory

    monkeypatch.setattr(local_env_import, "repo_root", lambda: tmp_path)
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / "import.env"))
    for name in local_env_import._FIELDS:
        monkeypatch.delenv(name, raising=False)
    for names in providers_local.PROVIDER_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    reset_db_factory()
    yield
    reset_db_factory()


@pytest.mark.parametrize("domain", ["warehouse-inspections", "home-fitness"])
def test_local_content_errors_name_setup_without_exposing_provider_errors(local_content, monkeypatch, domain):
    from app.api.ops_api import _content_generation_error
    from app.content.agent import policy

    monkeypatch.setattr(policy, "content_blocked_message", lambda: "Turn on Content in Features, then request your draft again.")
    disabled = _content_generation_error(ValueError("content_disabled"))
    assert disabled.status_code == 409
    assert disabled.detail["message"] == policy.content_blocked_message()
    failure = _content_generation_error(ValueError(f"{domain}: synthetic-private-key"))
    assert failure.detail["code"] == "content_generation_unavailable"
    assert "Keys and connections" in failure.detail["message"]
    assert "synthetic-private-key" not in str(failure.detail)
    assert _content_generation_error(ValueError("content_not_found")).status_code == 404
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert _content_generation_error(ValueError("content_disabled")).detail == {"code": "content_disabled"}


@pytest.mark.parametrize("ready", [True, False])
def test_local_content_gates_use_owner_capability(local_content, monkeypatch, ready):
    from app.content.agent import policy
    from app.marketing import executor

    module = ModuleType("app.core.local_capabilities")
    calls = []
    module.enabled = lambda name: calls.append(name) or ready
    module.blocked_message = lambda name: "Turn on Content in Features, then request your draft again."
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CONTENT_AGENT_ENABLED", "false" if ready else "true")
    monkeypatch.setenv("MARKETING_EXECUTION_ENABLED", "false" if ready else "true")
    assert policy.agent_enabled() is ready
    assert executor.execution_enabled() is ready
    assert calls == ["content", "content"]
    monkeypatch.setattr(executor.store, "active_plan", lambda wid: None if ready else pytest.fail("Work started while off"))
    result = asyncio.run(executor.run_tick(workspace_id="workspace"))
    assert result["enabled"] is ready
    if not ready:
        assert result["next_step"] == module.blocked_message("content")


@pytest.mark.parametrize("configured", [True, False])
def test_local_content_gate_fallback_uses_saved_model(local_content, monkeypatch, configured):
    from app.content.agent.policy import agent_enabled
    from app.core import providers_local

    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    monkeypatch.setenv("CONTENT_AGENT_ENABLED", "false" if configured else "true")
    if configured:
        providers_local.save_settings(provider="openai", api_key="synthetic-model-key", models={"chat": "gpt-4o-mini"})
    assert agent_enabled() is configured


@pytest.mark.parametrize("platform", ["linkedin", "devto"])
@pytest.mark.parametrize("name,audience,description", [
    ("Ledger review", "Small accounting teams", "A proposed invoice review tool. You would compare invoices with purchase orders before recording a payment."),
    ("Trail journal", "Weekend hikers", "A proposed walking journal. You would record the route, weather and your own observations after a walk."),
])
def test_local_topics_draft_review_and_markdown_with_offline_model(local_content, monkeypatch, platform, name, audience, description):
    from core.clock import utcnow
    from llm_gateway import cassette, sole
    from llm_gateway.client import LLMClient
    from sqlalchemy import select

    from app.content import brief, generation
    from app.core import providers_local
    from app.db.models import ContentAccount, Job, Venture
    from app.state import db
    from app.tenancy import bind
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    providers_local.save_settings(provider="openai", api_key="synthetic-model-key", models={"chat": "gpt-4o-mini"})
    module = ModuleType("app.core.local_capabilities")
    module.enabled = lambda capability: capability == "content"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    desk = ensure_desk()
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_content", workspace_id=desk.id, name=name, raw_idea=description,
            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.commit()
    saved = brief.save(workspace_id=desk.id, venture_id="ven_content", data={
        "product": {"name": name, "one_liner": description},
        "icp": {"segments": [{"name": audience, "pains": ["Keeping an accurate record"]}]},
        "claims": [], "competitors": [], "voice": {}, "cadence": {},
    })
    body = description + "\n\n" + (
        "This is a proposal, not a claim that the product is already available. "
        "Before you adopt a new tool, write down what you need to record, which details you check "
        "and where you keep the original information. Compare the proposed workflow with your current "
        "one. Which part would you keep doing yourself, and which part would you want help with?"
    )
    fixtures = {
        "content.topics": {"topics": [{"angle": "Review your records", "hook": "What do you check?",
            "rationale": "Explore the proposed workflow", "icp_segment": audience, "intent": "value",
            "evidence": ["product"], "score": .8}]},
        "content.writer": {"body": body, "title": "", "visual": None},
        "content.critic": {"score": .9, "verdict": "publish", "problems": [], "unsupported_claims": [],
            "reads_as_ai": False, "claims_reviewed": True, "claim_checks": [{"draft_quote": description,
                "status": "proposal", "evidence": [{"source_id": "product", "quote": description}],
                "reason": "The proposal preserves the stated product stage."}]},
    }
    for purpose, output in fixtures.items():
        monkeypatch.setitem(cassette.DETERMINISTIC_FIXTURES, purpose,
            {"content": json.dumps(output), "tokens_in": 100, "tokens_out": 180})
    calls = []

    async def offline_transport(**kwargs):
        calls.append(kwargs["purpose"])
        output = await LLMClient(config=kwargs["config"], offline=True).chat(kwargs["messages"], purpose=kwargs["purpose"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output.content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=output.tokens_in, completion_tokens=output.tokens_out,
                total_tokens=output.tokens_in + output.tokens_out), model="gpt-4o-mini")

    monkeypatch.setattr(sole, "transmit", offline_transport)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")

    def generate(kind):
        request = {"kind": kind, "brief_id": saved["id"], **({"platform": platform} if kind == "draft" else {"count": 1})}
        quote = generation.quote(desk.id, "ven_content", request)
        assert quote["payer"] == "founder" and quote["sufficient_now"]
        run = generation.start(desk.id, "ven_content", request, idempotency_key="request-" + kind,
            **{field: quote[field] for field in ("input_digest", "resource_digest", "ceiling_micro")})
        with db()() as session:
            job = session.get(Job, run["job_id"])
            job.status, job.locked_by, job.attempts = "leased", "test-worker", 1
            payload = json.loads(job.payload_json)
            payload.update(workspace_id=desk.id, venture_id="ven_content", _job_id=job.id,
                _worker_id=job.locked_by, _job_attempt=job.attempts)
            session.commit()
        return (generation.run_topics if kind == "topics" else generation.run_draft)(payload)

    with bind(workspace_id=desk.id, venture_id="ven_content", actor=LOCAL_PERSON_ID):
        assert generate("topics")["state"] == "completed"
        assert generate("draft")["state"] == "completed"
        post = generation.posts(desk.id, "ven_content")[0]
        monkeypatch.setattr(module, "enabled", lambda capability: False)
        with pytest.raises(ValueError, match="content_disabled"):
            generation.quote(desk.id, "ven_content", {"kind": "topics", "brief_id": saved["id"]})
    assert calls == ["content.topics", "content.writer", "content.critic"]
    assert post["body"] == body and post["state"] == "draft"
    assert post["scheduled_for"] is None and post["visual"] == {}
    assert post["critic"]["claims_reviewed"] is True
    assert post["handoff"]["text"] == body
    assert post["handoff"]["markdown"] == body + "\n"
    assert post["handoff"]["filename"].endswith(".md")
    assert "publish it yourself" in post["handoff"]["next_step"]
    assert "Media Studio" in post["handoff"]["image_next_step"]
    with db()() as session:
        assert session.scalar(select(ContentAccount.id)) is None
    from app.credits.local_meter import snapshot

    assert snapshot()["month_cost_usd_micro"] > 0
    assert len(snapshot()["tasks"]) == 3


@pytest.mark.parametrize("domain", ["warehouse-inspections", "home-fitness"])
def test_local_image_keys_are_saved_not_environment(local_content, monkeypatch, tmp_path, domain):
    from app.core.local_secrets import update_settings
    from app.marketing.media import images, probes

    assert images._bfl_key() == ""
    assert not probes.pexels_available()["configured"]
    assert not images._pollinations_enabled()
    keys = {name: domain + "-synthetic-" + name for name in
        ("bfl_api_key", "gemini_api_key", "pexels_api_key", "pixabay_api_key")}
    update_settings("images", {"comfyui_url": "http://127.0.0.1:8189"},
        keys)
    for name in ("BFL_API_KEY", "GEMINI_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY"):
        monkeypatch.setenv(name, "unused-environment-value")
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8190")
    assert images._bfl_key() == keys["bfl_api_key"]
    assert images._gemini_key() == keys["gemini_api_key"]
    assert images._comfy_base() == "http://127.0.0.1:8189"
    assert probes.pexels_available()["configured"] and probes.pixabay_available()["configured"]
    assert probes._stock_key("pexels") == keys["pexels_api_key"]
    assert probes._stock_key("pixabay") == keys["pixabay_api_key"]
    assert not images._pollinations_enabled()
    stored = (tmp_path / "local-settings.json").read_text()
    assert all(key not in stored for key in keys.values())
    assert not (tmp_path / "import.env").exists()


@pytest.mark.parametrize("system,command", [("Darwin", "brew install ffmpeg"),
    ("Windows", "winget install --id Gyan.FFmpeg"), ("Linux", "sudo apt-get install ffmpeg")])
def test_missing_ffmpeg_names_os_install_command(local_content, monkeypatch, system, command):
    from app.marketing.media import video

    monkeypatch.setattr(video, "ffmpeg_bin", lambda: None)
    monkeypatch.setattr(host_platform, "system", lambda: system)
    monkeypatch.setattr(video.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    monkeypatch.setattr(video.media_store, "list_assets", lambda *args, **kwargs: pytest.fail("Encoding started without ffmpeg"))
    probe = video.ffmpeg_available()
    result = video.generate_video(workspace_id="workspace", headline="Your clip")
    assert not probe["ok"] and result["status"] == "blocked"
    assert command in probe["next_step"]
    assert probe["note"] == probe["next_step"]
    assert result["block_reason"] == probe["next_step"]

"""GTM drafting uses the founder wallet and workspace-scoped provider gateway."""

from __future__ import annotations

from types import SimpleNamespace
import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def _fresh_db(monkeypatch, tmp_path, workspace_id: str = "ws_draft"):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/draft.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/draft.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    from app.db.models import Base, CreditAccount, Person, Workspace
    from app.state import db, reset_db_factory
    from core.clock import utcnow

    reset_db_factory()
    factory = db()
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
        now = utcnow()
        session.add(Person(id="per_draft", display_name="Draft founder", email="draft@example.com", created_at=now))
        session.add(
            Workspace(
                id=workspace_id, kind="company", name="Draft workspace", entry_mode="idea",
                owner_id="per_draft", tenancy="hosted", autonomy_level="L1", observe_level="O0",
                created_at=now, updated_at=now,
            )
        )
        session.add(
            CreditAccount(
                id="cra_draft",
                scope="person",
                venture_id=None,
                workspace_id=None,
                owner_person_id="per_draft",
                granted_micro=2_000_000,
                spent_micro=0,
                reserved_micro=0,
                status="active",
                version=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return factory


def _completion(content: str, *, provider: str = "foundry") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40, total_tokens=160),
        model="DeepSeek-V4-Pro",
        provider_used=provider,
    )


def _draft(workspace_id: str | None, **overrides):
    from app.gtm.supymem.draft import draft_step

    params = dict(
        first_name="Ada",
        email="ada@example.com",
        company="Analytical",
        title="Founder",
        hook="got into YC W26",
        linkedin_url=None,
        research_notes="Public YC W26 announcement.",
        day=1,
        workspace_id=workspace_id,
    )
    return draft_step(**(params | overrides))


@pytest.mark.parametrize("evidence,allowed", [
    ({"kind": "published_company_contact", "url": "https://sample-analytics.example.com/contact"}, True),
    ({"kind": "published_company_contact", "url": "https://other-company.example.com/contact"}, False),
    ({"kind": "guessed", "url": "https://sample-analytics.example.com/contact"}, False),
    (None, False),
])
def test_real_draft_boundary_preserves_only_published_recipient(monkeypatch, tmp_path, evidence, allowed):
    _fresh_db(monkeypatch, tmp_path)
    from app.boundary.filter import inspect_payload
    from llm_gateway.boundary import register_filter
    from llm_gateway import sole

    seen = []

    def complete(messages, kwargs):
        seen.extend(messages)
        return _completion('{"subject":"Congrats","body":"Hi Ada, congratulations on YC W26.","hook":"got into YC W26"}')

    async def primary(messages, **kwargs):
        return complete(messages, kwargs)

    from app.core.local_mode import is_local
    if is_local():
        from tests.idea_fixtures import install_provider
        install_provider(monkeypatch, complete)
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")

    register_filter(inspect_payload)
    sole.set_transmit_hooks(primary=primary)
    try:
        _draft("ws_draft", email="ada@sample-analytics.example.com", contact_evidence=evidence,
            research_notes="Public announcement. Other contact: bob@other-company.example.com.")
    finally:
        sole.set_transmit_hooks(primary=None, fallback=None)
        register_filter(None)
    assert seen
    prompt = seen[-1]["content"]
    assert ("ada@sample-analytics.example.com" in prompt) is allowed
    assert "bob@other-company.example.com" not in prompt


@pytest.mark.hosted_only
def test_draft_uses_scoped_foundry_and_settles_workspace_credit(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from app.credits.accounts import get_account_by_id
    from llm_gateway.boundary import Decision, register_filter
    from llm_gateway import sole

    calls: list[dict] = []

    async def primary(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return _completion(
            '{"subject":"Congrats on YC W26","body":"Hey Ada,\\n\\nSaw you got into YC W26. Congratulations on the milestone.\\n","hook":"got into YC W26"}'
        )

    register_filter(lambda messages, **_kwargs: Decision(messages=list(messages)))
    sole.set_transmit_hooks(primary=primary)
    try:
        step = _draft("ws_draft")
    finally:
        sole.set_transmit_hooks(primary=None, fallback=None)
        register_filter(None)

    assert step["source"] == "llm"
    assert step["llm_status"] == "llm_completed"
    assert step["llm_provider"] == "foundry"
    assert calls and calls[0]["purpose"] == "gtm.draft_post"
    assert calls[0]["workspace_id"] == "ws_draft"
    account = get_account_by_id("cra_draft")
    assert account is not None
    assert account.spent_micro > 0
    assert account.reserved_micro == 0


@pytest.mark.hosted_only
def test_draft_provider_failure_is_template_with_provenance_and_releases_hold(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from app.credits.accounts import get_account_by_id
    from llm_gateway.boundary import Decision, register_filter
    from llm_gateway.errors import ProviderCallError
    from llm_gateway import sole

    async def primary(*_args, **_kwargs):
        raise ProviderCallError("rate_limited", "try later", status=429, retryable=True)

    monkeypatch.delenv("LLM_FALLBACK_PROVIDER", raising=False)
    register_filter(lambda messages, **_kwargs: Decision(messages=list(messages)))
    sole.set_transmit_hooks(primary=primary)
    try:
        step = _draft("ws_draft")
    finally:
        sole.set_transmit_hooks(primary=None, fallback=None)
        register_filter(None)

    assert step["source"] == "template"
    assert step["llm_status"] == "llm_provider_rate_limited"
    account = get_account_by_id("cra_draft")
    assert account is not None
    assert account.spent_micro == 0
    assert account.reserved_micro == 0


@pytest.mark.hosted_only
def test_platform_draft_without_credit_account_never_dials(monkeypatch, tmp_path):
    # Create schema only; a platform model call needs a workspace credit account.
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/no_account.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/no_account.db")
    from app.db.models import Base
    from app.state import db, reset_db_factory
    from llm_gateway import sole

    reset_db_factory()
    with db()() as session:
        Base.metadata.create_all(session.get_bind())
    dialed = False

    async def primary(*_args, **_kwargs):
        nonlocal dialed
        dialed = True
        return _completion("{}")

    sole.set_transmit_hooks(primary=primary)
    try:
        step = _draft("ws_no_account")
    finally:
        sole.set_transmit_hooks(primary=None, fallback=None)

    assert step["source"] == "template"
    assert step["llm_status"] == "llm_credit_account_unavailable"
    assert dialed is False


def test_founder_byok_uses_workspace_resolver_without_platform_reservation(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path, "ws_byok")
    from app.llm.founder_provider import set_founder_config
    from llm_gateway.boundary import Decision, register_filter
    from llm_gateway import sole

    set_founder_config(
        workspace_id="ws_byok",
        provider="openai_compatible",
        base_url="https://founder.example/v1",
        model="founder-model",
        api_key="founder-secret",
    )
    seen: dict[str, object] = {}

    async def byok(messages, *, config, **kwargs):
        seen["config"] = config
        seen["workspace_id"] = kwargs["workspace_id"]
        return _completion(
            '{"subject":"Congrats on YC W26","body":"Hey Ada,\\n\\nSaw you got into YC W26. Congratulations on the milestone.\\n","hook":"got into YC W26"}',
            provider="byok",
        )

    register_filter(lambda messages, **_kwargs: Decision(messages=list(messages)))
    monkeypatch.setattr(sole, "byok_chat_completion", byok)
    from app.core.local_mode import is_local
    if is_local():
        from app.core import providers_local
        providers_local.save_settings(provider="openai_compatible", base_url="https://founder.example/v1",
            models={"chat": "founder-model"}, api_key="founder-secret")
        monkeypatch.setenv("LLM_OFFLINE", "0")
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    try:
        step = _draft("ws_byok")
    finally:
        register_filter(None)

    assert step["source"] == "llm"
    assert step["llm_provider"] == "byok"
    assert seen["workspace_id"] == "ws_byok"
    assert seen["config"].deployment_pro == "founder-model"
    from app.credits.accounts import get_account_by_id
    account = get_account_by_id("cra_draft")
    assert account is not None
    assert account.spent_micro == 0
    assert account.reserved_micro == 0


def test_draft_has_no_direct_vendor_http_or_key_path():
    from app.gtm.supymem import draft

    source = open(draft.__file__, encoding="utf-8").read()
    assert "ANTHROPIC_API_KEY" not in source
    assert "api.anthropic.com" not in source
    assert "httpx" not in source
    assert "sole.transmit" in source


@pytest.mark.parametrize("company,description", [
    ("Foundry scheduling", "Scheduling machine inspections for manufacturing teams"),
    ("Garden journal", "Tracking seasonal planting for home gardeners"),
])
def test_local_saved_provider_drafts_without_a_hosted_wallet(monkeypatch, tmp_path, company, description):
    import json

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    factory = _fresh_db(monkeypatch, tmp_path)
    from app.db.models import CreditAccount
    from tests.idea_fixtures import install_provider

    with factory() as session:
        session.delete(session.get(CreditAccount, "cra_draft"))
        session.commit()
    calls = []

    def complete(messages, options):
        calls.append(options)
        return _completion(json.dumps({"subject": company,
            "body": f"Hi Ada, I read your note about {description}. Could you share your recent experience?", "hook": description}))

    install_provider(monkeypatch, complete)
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    step = _draft("ws_draft", company=company, hook=description, research_notes=description)
    assert step["source"] == "llm" and len(calls) == 1
    assert calls[0]["workspace_id"] == "ws_draft"
    from app.credits import local_meter
    with local_meter._database() as connection:
        receipts = connection.execute("SELECT status FROM calls WHERE workspace_id = ? AND purpose = ?",
            ("ws_draft", "gtm.draft_post")).fetchall()
    assert [receipt["status"] for receipt in receipts] == ["confirmed"]


@pytest.mark.parametrize("description", ["Machine inspections for manufacturers", "Seasonal planting for home gardeners"])
@pytest.mark.parametrize("classified", [True, False])
def test_local_draft_rejection_keeps_its_classification_private(monkeypatch, tmp_path, caplog, description, classified):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _fresh_db(monkeypatch, tmp_path)
    from app.credits import metered
    from app.gtm.supymem.draft import _canonical_complete
    from llm_gateway.errors import ProviderCallError
    from tests.idea_fixtures import install_provider

    install_provider(monkeypatch)
    async def reject(*args, **kwargs):
        if classified:
            raise ProviderCallError("bad_api_key", "private-provider-response", status=401)
        raise RuntimeError("private-provider-response")
    monkeypatch.setattr(metered, "llm", reject)
    text, result = _canonical_complete(description, workspace_id="ws_draft")
    assert text is None and result["status"] == ("llm_provider_bad_api_key" if classified else "llm_capability_unavailable")
    assert "private-provider-response" not in caplog.text and "private-provider-response" not in str(result)


@pytest.mark.parametrize("venture_id", ["machine_inspections", "home_gardening"])
def test_sync_draft_bridge_keeps_tenant_and_spend_context(venture_id):
    import asyncio

    from app.credits.gate import _call_allowance, bounded_calls
    from app.gtm.supymem.draft import _run_async
    from app.tenancy import bind, current_or_none

    def admit(*args, **kwargs):
        pytest.fail("Reading your bound context must not spend anything.")

    async def read_context():
        return current_or_none(), _call_allowance.get()

    async def run():
        with bind(workspace_id="ws_draft", venture_id=venture_id, actor="per_draft") as expected:
            with bounded_calls("ws_draft", venture_id, admit):
                scope, allowance = _run_async(read_context())
            assert scope == expected and allowance == ("ws_draft", venture_id, admit)

    asyncio.run(run())


def test_normalise_note_replaces_dashes_with_plain_punctuation():
    from app.gtm.discovery import normalise_note
    assert normalise_note("Please keep replies general—no personal details needed.") == "Please keep replies general, no personal details needed."
    assert normalise_note("Hi team, quick question – does this apply? Reply with a time") == "Hi team,\n\nquick question, does this apply? Reply with a time."

"""Scoped declared resources; isolated database and provider-metadata fixtures."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def resource_module(tmp_path_factory):
    root = tmp_path_factory.mktemp("resource_module")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COMPANY_OS_DATA_DIR", str(root))
        patch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(root / "memory.db"))
        patch.setenv("LLM_OFFLINE", "1")
        load_company_os_app()
        from app.planning import resources

        yield resources


@pytest.fixture
def resources(resource_module, tmp_path, monkeypatch):
    mod = resource_module
    from app.db.models import CreditPolicy
    from app.core.providers_local import save_settings
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with monkeypatch.context() as local:
        local.setenv("SUPYMEM_MODE", "local")
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234", models={"chat": "test-model"})
    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "build")  # the existing contract below is the build lane
    engine = create_engine(f"sqlite:///{tmp_path}/resources.db")
    for model in (mod.Workspace, mod.Venture, mod.CreditAccount, CreditPolicy, mod.ExistingAsset):
        model.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(mod, "db", lambda: factory)
    monkeypatch.setattr(
        mod,
        "resolve_llm_config",
        lambda workspace_id: SimpleNamespace(
            provider="azure_foundry",
            model="deployment-pro",
            source="platform_default",
            api_key_ref=None,
            base_url="https://private.example?api_key=never-output",
        ),
    )
    now = mod.utcnow()
    with factory() as session:
        session.add(CreditPolicy(key="founder_wallet_phase", value_json='"legacy"',
            updated_at=now, updated_by="synthetic-resource-fixture"))
        for wid in ("ws_a", "ws_b"):
            session.add(
                mod.Workspace(
                    id=wid,
                    kind="venture",
                    name=wid,
                    entry_mode="idea",
                    owner_id="person",
                    created_at=now,
                    updated_at=now,
                )
            )
        for vid, wid in (("ven_a", "ws_a"), ("ven_sibling", "ws_a"), ("ven_b", "ws_b")):
            session.add(
                mod.Venture(
                    id=vid,
                    workspace_id=wid,
                    name=vid,
                    raw_idea="A manual pilot",
                    stage="draft",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    yield mod, factory
    engine.dispose()


def declaration(**changes):
    item = {
        "kind": "coding_agent",
        "name": "Cursor",
        "exists": True,
        "detail": {"provider": "Cursor", "plan": "Founder-reported subscription"},
    }
    item.update(changes)
    return item


@pytest.mark.parametrize("idea", ["Stock forecasting for hardware distributors", "Practice plans for amateur runners"])
def test_local_resources_never_wait_for_a_managed_builder(resources, monkeypatch, idea):
    mod, factory = resources
    from app import coding_agents_consent
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with factory() as session:
        session.get(mod.Venture, "ven_a").raw_idea = idea
        session.commit()
    from app.builder import budgets
    monkeypatch.setattr(budgets, "runtime_quote", lambda *_: pytest.fail("Local planning must not quote a managed builder"))
    unavailable = next(item for item in mod.snapshot("ws_a", "ven_a")["capabilities"] if item["id"] == "firstlook.landing")
    assert unavailable["state"] == "unavailable" and "Settings > Keys and connections > Coding tools" in unavailable["reason"]
    monkeypatch.setattr(coding_agents_consent, "consents", lambda _: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    result = mod.snapshot("ws_a", "ven_a")
    assert result["execution_budget"]["status"] == "local"
    landing = next(item for item in result["capabilities"] if item["id"] == "firstlook.landing")
    assert landing["state"] == "implemented" and "your connected coding tool" in landing["reason"]
    assert landing["metered"] is False and landing["provider_configuration"] is None
    assert "hosted" not in " ".join(result["execution_budget"]["gaps"]).lower()


@pytest.mark.parametrize("idea", ["Logistics quoting for distributors", "Weekend craft workshops for families"])
@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_local_publication_reads_saved_connections(resources, monkeypatch, idea, provider):
    mod, factory = resources
    from app.core import local_capabilities

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with factory() as session:
        session.get(mod.Venture, "ven_a").raw_idea = idea
        session.commit()
    settings = {"capabilities": {"publish_pages": True}, "publishing": {
        "provider": provider, "project": "owner-project", "bucket": "owner-bucket",
        "region": "us-east-1", "prefix": "pages", "site_url": "https://pages.example.test"}}
    monkeypatch.setattr(local_capabilities, "read_settings", lambda section: {"values": settings.get(section, {})})
    requested = []

    def get_secret(section, name):
        requested.append((section, name))
        return "synthetic-credential"

    monkeypatch.setattr(local_capabilities, "get_secret", get_secret)
    result = mod.publication_contract("ven_a")
    assert result["configured"] and result["kind"] == "local_publication"
    assert requested == ([("publishing", "access_key_id"), ("publishing", "secret_access_key")]
        if provider == "s3" else [("publishing", "token")])
    assert "synthetic-credential" not in json.dumps(result)
    settings["capabilities"]["publish_pages"] = False
    blocked = mod.publication_contract("ven_a")
    assert not blocked["configured"] and "Settings > Capabilities" in blocked["steps"][-1]


@pytest.mark.hosted_only
def test_snapshot_is_truthful_with_unknown_budget_and_no_account_creation(resources):
    mod, factory = resources
    result = mod.snapshot("ws_a", "ven_a")
    assert result["inventory"] == {"complete": False, "assets": []}
    assert result["credit_account"] == {
        "state": "missing",
        "available_micro": None,
        "reserved_micro": None,
        "spent_micro": None,
    }
    assert result["execution_budget"]["status"] == "unknown"
    assert result["execution_budget"]["upfront_credits"] is None
    assert result["execution_budget"]["monthly_credits"] is None
    assert result["execution_budget"]["gaps"]
    with factory() as session:
        assert session.scalar(select(mod.CreditAccount)) is None
        assert session.scalar(select(mod.ExistingAsset)) is None


def test_capabilities_describe_code_support_without_ga_or_provider_health_claims(
    resources,
):
    mod, _ = resources
    rows = {row["id"]: row for row in mod.snapshot("ws_a", "ven_a")["capabilities"]}
    if mod.is_local():
        assert set(rows) == {*mod.SUPPORTED_CAPABILITIES, "firstlook.landing"}
        for capability in mod.SUPPORTED_CAPABILITIES:
            assert rows[capability]["state"] == "implemented"
            assert rows[capability]["metered"] is True
            assert rows[capability]["provider_configuration"]["health"] == "unverified"
        assert rows["firstlook.landing"]["supported_milestone_kinds"] == ["landing"]
        assert "private.example" not in json.dumps(rows)
        assert "never-output" not in json.dumps(rows)
        return
    assert set(rows) == {
        "idea.clarify_turn",
        "idea.validate",
        "research.deep",
        "plan.compile",
        "build.run",
        "coding.subscription_static",
        "coding.subscription_product_source",
        "deploy.run",
        "infra.provision",
        "app.iterate",
    }
    for capability in mod.SUPPORTED_CAPABILITIES:
        assert rows[capability]["state"] == "implemented"
        assert rows[capability]["metered"] is True
        assert rows[capability]["provider_configuration"]["health"] == "unverified"
    for capability in mod.UNAVAILABLE_CAPABILITIES:
        assert rows[capability]["state"] == "unavailable"
        assert rows[capability]["metered"] is False
        assert rows[capability]["reason"]
    assert "private.example" not in json.dumps(rows)
    assert "never-output" not in json.dumps(rows)


def test_ready_builder_is_one_build_qa_artifact_review_contract(resources, monkeypatch):
    mod, _ = resources
    from app.builder import budgets

    monkeypatch.setattr(
        budgets,
        "runtime_quote",
        lambda _workspace_id: {
            "ready": True,
            "ceiling_micro": 120_000,
        },
    )
    result = mod.snapshot("ws_a", "ven_a")
    rows = {row["id"]: row for row in result["capabilities"]}

    assert "qa.run" not in rows
    if mod.is_local():
        assert rows["firstlook.landing"]["state"] == "unavailable"
        assert "build.run" not in rows and "deploy.run" not in rows
        assert result["execution_budget"]["status"] == "local"
        return
    assert rows["build.run"]["state"] == "implemented"
    assert rows["build.run"]["included_work"] == [
        "isolated static landing or authenticated core-product implementation",
        "profile-specific independent QA",
        "immutable artifact capture",
        "private founder review",
        "accepted core-product staging through isolated managed runtime",
    ]
    assert rows["build.run"]["supported_milestone_kinds"] == ["landing", "core"]
    assert rows["build.run"]["excluded_work"] == [
        "product iteration",
        "unverified third-party integration",
        "public publication before runtime QA and founder review",
        "payment implementation",
    ]
    assert rows["deploy.run"]["state"] == "unavailable"
    assert result["execution_budget"]["status"] == "bounded"


@pytest.mark.parametrize("valid", [True, False])
def test_static_publication_configuration_does_not_unlock_legacy_executor(resources, monkeypatch, valid):
    mod, _ = resources
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_SITE_S3_BUCKET", "reviewed-public-sites")
    monkeypatch.setenv("PUBLIC_SITE_AWS_REGION", "us-east-1")
    monkeypatch.setenv("PUBLIC_SITE_S3_EXPECTED_OWNER", "123456789012")
    monkeypatch.setenv("PUBLIC_SITE_KVS_ARN", "arn:aws:cloudfront::123456789012:key-value-store/test" if valid else "invalid")
    monkeypatch.setenv("PUBLIC_SITE_DOMAIN", "supymem.com")
    result = mod.snapshot("ws_a", "ven_a")
    publication = result["managed_publication"]
    assert publication["configured"] is valid
    assert publication["provider_health"] == "unverified"
    assert publication["review_path"] == "/v/ven_a/site"
    assert "123456789012" not in json.dumps(result)
    assert "reviewed-public-sites" not in json.dumps(result)
    assert next(row for row in result["capabilities"] if row["id"] == "deploy.run")["state"] == "unavailable"
    from app.core.jobs import FatalError
    from app.worker.handlers import require_capability
    with pytest.raises(FatalError, match="capability_unavailable:deploy.run"):
        require_capability("deploy.run")


def test_completed_inventory_persists_marker_and_unknown_verification_fields(resources):
    mod, factory = resources
    result = mod.update_inventory("ws_a", "ven_a", [declaration()], True)
    assert result["complete"] is True
    assert len(result["assets"]) == 1
    asset = result["assets"][0]
    assert asset["source"] == "founder_inventory"
    assert asset["usable_tasks"] == []
    for field in (
        "accessible",
        "compatible",
        "usable",
        "credits_saved_estimate",
        "verified_at",
    ):
        assert asset[field] is None
    assert mod.snapshot("ws_a", "ven_a")["inventory"] == result
    with factory() as session:
        marker = session.get(mod.ExistingAsset, mod._marker_id("ws_a", "ven_a"))
        assert marker.kind == "resource_inventory"
        assert marker.name == "Founder resource inventory"
        assert json.loads(marker.detail)["completed_at"]


def test_empty_inventory_can_explicitly_be_completed_and_reopened(resources):
    mod, _ = resources
    assert mod.update_inventory("ws_a", "ven_a", [], True) == {
        "complete": True,
        "assets": [],
    }
    assert mod.update_inventory("ws_a", "ven_a", [], False) == {
        "complete": False,
        "assets": [],
    }


def test_own_declarations_are_updated_and_omitted_declarations_removed(resources):
    mod, factory = resources
    first = mod.update_inventory(
        "ws_a",
        "ven_a",
        [declaration(), declaration(kind="repo", name="Existing code", detail={})],
        True,
    )
    asset_id = next(
        asset["id"] for asset in first["assets"] if asset["kind"] == "coding_agent"
    )
    result = mod.update_inventory(
        "ws_a", "ven_a", [declaration(id=asset_id, name="Codex", exists=False)], True
    )
    assert len(result["assets"]) == 1
    assert result["assets"][0]["id"] == asset_id
    assert result["assets"][0]["name"] == "Codex"
    assert result["assets"][0]["exists"] is False
    with factory() as session:
        assert len(session.scalars(select(mod.ExistingAsset)).all()) == 2


def test_unchanged_kind_and_name_keep_stable_id_without_supplied_id(resources):
    mod, _ = resources
    first = mod.update_inventory("ws_a", "ven_a", [declaration()], False)
    second = mod.update_inventory("ws_a", "ven_a", [declaration()], True)
    assert first["assets"][0]["id"] == second["assets"][0]["id"]


def test_imported_rows_survive_replacement_and_unsafe_import_detail_is_not_returned(
    resources,
):
    mod, factory = resources
    with factory() as session:
        session.add(
            mod.ExistingAsset(
                id="imported",
                workspace_id="ws_a",
                venture_id="ven_a",
                kind="repo",
                name="Existing repository",
                source="import",
                exists=True,
                detail=json.dumps(
                    {
                        "root": "/private/path",
                        "token": "secret-value",
                        "provider": "github",
                    }
                ),
                created_at=mod.utcnow(),
            )
        )
        session.commit()
    mod.update_inventory("ws_a", "ven_a", [declaration()], True)
    result = mod.update_inventory("ws_a", "ven_a", [], True)
    assert [asset["id"] for asset in result["assets"]] == ["imported"]
    assert result["assets"][0]["detail"] == {"provider": "github"}
    assert "/private/path" not in json.dumps(result)
    assert "secret-value" not in json.dumps(result)
    with pytest.raises(ValueError, match="not found"):
        mod.update_inventory("ws_a", "ven_a", [declaration(id="imported")], True)


@pytest.mark.parametrize(
    "ws,vid",
    [
        ("ws_b", "ven_a"),
        ("ws_a", "ven_b"),
        ("missing", "ven_a"),
        ("ws_a", "missing"),
        ("", "ven_a"),
        ("ws_a", "../ven_a"),
    ],
)
def test_foreign_missing_or_invalid_scope_is_rejected_before_inventory_read_or_write(
    resources, ws, vid
):
    mod, factory = resources
    for operation in (
        lambda: mod.snapshot(ws, vid),
        lambda: mod.update_inventory(ws, vid, [declaration()], True),
    ):
        with pytest.raises(ValueError):
            operation()
    with factory() as session:
        assert session.scalar(select(mod.ExistingAsset)) is None


@pytest.mark.parametrize(
    "model_name,id_value", [("Workspace", "ws_a"), ("Venture", "ven_a")]
)
def test_archived_scope_rejected(resources, model_name, id_value):
    mod, factory = resources
    with factory() as session:
        session.get(getattr(mod, model_name), id_value).archived_at = mod.utcnow()
        session.commit()
    with pytest.raises(ValueError, match="not found"):
        mod.snapshot("ws_a", "ven_a")


@pytest.mark.parametrize("ws,vid", [("ws_a", "ven_sibling"), ("ws_b", "ven_b")])
def test_asset_ids_from_other_ventures_or_workspaces_are_rejected_atomically(
    resources, ws, vid
):
    mod, _ = resources
    original = mod.update_inventory("ws_a", "ven_a", [declaration()], True)
    alien = mod.update_inventory(ws, vid, [declaration(name="Other tool")], True)[
        "assets"
    ][0]["id"]
    with pytest.raises(ValueError, match="not found"):
        mod.update_inventory("ws_a", "ven_a", [declaration(id=alien)], False)
    assert mod.snapshot("ws_a", "ven_a")["inventory"] == original


def test_unknown_asset_ids_cannot_create_arbitrary_records(resources):
    mod, _ = resources
    with pytest.raises(ValueError, match="not found"):
        mod.update_inventory("ws_a", "ven_a", [declaration(id="unallocated")], True)


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "resource_inventory"},
        {"kind": "unknown"},
        {"name": ""},
        {"name": "x" * 121},
        {"exists": "yes"},
        {"accessible": True},
        {"usable": True},
        {"credits_saved_estimate": 100},
        {"detail": {"api_key": "do-not-store"}},
        {"detail": {"token": "do-not-store"}},
        {"detail": {"nested": {"password": "do-not-store"}}},
        {"detail": {"expires_on": "2026-99-99"}},
        {"detail": {"hours_per_week": 169}},
        {"detail": {"credit_amount": "-5"}},
        {"detail": {"credit_amount": 500}},
        {"detail": {"notes": "x" * 501}},
    ],
)
def test_declarations_allow_only_bounded_typed_metadata(resources, patch):
    mod, factory = resources
    with pytest.raises(ValueError, match="declaration is invalid"):
        mod.update_inventory("ws_a", "ven_a", [declaration(**patch)], True)
    with factory() as session:
        assert session.scalar(select(mod.ExistingAsset)) is None


@pytest.mark.parametrize(
    "secret",
    [
        "sk-live-test-value-1234567890",
        "ghp_1234567890abcdefghij",
        "password=do-not-store-password",
        "-----BEGIN PRIVATE KEY-----secret",
    ],
)
def test_secret_shaped_values_in_names_and_notes_are_refused_without_echoing(
    resources, secret
):
    mod, factory = resources
    for asset in (declaration(name=secret), declaration(detail={"notes": secret})):
        with pytest.raises(ValueError) as error:
            mod.update_inventory("ws_a", "ven_a", [asset], True)
        assert secret not in str(error.value)
    with factory() as session:
        assert session.scalar(select(mod.ExistingAsset)) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/repo",
        "https://example.com/repo?token=private",
        "https://example.com/#secret",
        "file:///private/repo",
        "javascript:alert(1)",
        "https://example.com:443/repo",
        "https://evil.example\\github.com/repo",
        "https://example.com/%67%68%70_1234567890abcdefghij",
    ],
)
def test_resource_urls_cannot_embed_credentials_or_non_http_payloads(resources, url):
    mod, _ = resources
    with pytest.raises(ValueError):
        mod.update_inventory(
            "ws_a", "ven_a", [declaration(kind="repo", detail={"url": url})], True
        )


def test_asset_count_completion_type_and_duplicates_are_bounded(resources):
    mod, _ = resources
    for assets, complete in (
        ([declaration()] * 41, True),
        ([], "true"),
        ([declaration(), declaration(name="cursor")], True),
    ):
        with pytest.raises(ValueError):
            mod.update_inventory("ws_a", "ven_a", assets, complete)


@pytest.mark.hosted_only
def test_declared_cloud_credits_are_not_treated_as_available_platform_credits(
    resources,
):
    mod, _ = resources
    inventory = mod.update_inventory(
        "ws_a",
        "ven_a",
        [
            declaration(
                kind="aws_credits",
                name="AWS credit claim",
                detail={
                    "provider": "AWS",
                    "credit_amount": "5000.00",
                    "currency": "USD",
                    "expires_on": "2027-01-01",
                },
            )
        ],
        True,
    )
    assert inventory["assets"][0]["credits_saved_estimate"] is None
    result = mod.snapshot("ws_a", "ven_a")
    assert result["credit_account"]["available_micro"] is None
    assert result["execution_budget"]["status"] == "unknown"


@pytest.mark.hosted_only
def test_credit_balance_uses_exact_venture_and_workspace_without_fallback(resources):
    mod, factory = resources
    now = mod.utcnow()
    with factory() as session:
        session.add(
            mod.CreditAccount(
                id="account_a",
                workspace_id="ws_a",
                venture_id="ven_a",
                scope="venture",
                granted_micro=10000,
                reserved_micro=2000,
                spent_micro=3000,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            mod.CreditAccount(
                id="account_sibling",
                workspace_id="ws_a",
                venture_id="ven_sibling",
                scope="venture",
                granted_micro=999999,
                reserved_micro=0,
                spent_micro=0,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    assert mod.snapshot("ws_a", "ven_a")["credit_account"] == {
        "state": "active",
        "available_micro": 5000,
        "reserved_micro": 2000,
        "spent_micro": 3000,
    }
    assert mod.snapshot("ws_b", "ven_b")["credit_account"]["state"] == "missing"
    with factory() as session:
        session.get(mod.CreditAccount, "account_a").workspace_id = "ws_b"
        session.commit()
    assert mod.snapshot("ws_a", "ven_a")["credit_account"]["state"] == "missing"


def test_byok_config_exposes_only_metadata_without_vault_access(resources, monkeypatch):
    mod, _ = resources
    calls = []

    def resolve(workspace_id):
        calls.append(workspace_id)
        return SimpleNamespace(
            provider="openai",
            model="configured-model",
            source="founder_override",
            api_key_ref="vault:private-handle",
            api_key="never-output",
            base_url="https://secret-host.example",
        )

    monkeypatch.setattr(mod, "resolve_llm_config", resolve)
    result = mod.snapshot("ws_a", "ven_a")
    metadata = result["capabilities"][0]["provider_configuration"]
    assert metadata == {
        "provider": "openai",
        "model": "configured-model",
        "credential_source": "workspace",
        "credential_state": "reference_present",
        "health": "unverified",
    }
    assert calls == ["ws_a"]
    assert "vault:private-handle" not in json.dumps(result)
    assert "never-output" not in json.dumps(result)
    assert "secret-host" not in json.dumps(result)


def test_configuration_errors_do_not_leak_provider_details(resources, monkeypatch):
    mod, _ = resources

    def broken(workspace_id):
        raise ValueError("sensitive-provider-configuration")

    monkeypatch.setattr(mod, "resolve_llm_config", broken)
    result = mod.snapshot("ws_a", "ven_a")
    assert (
        result["capabilities"][0]["provider_configuration"]["credential_source"]
        == "unknown"
    )
    assert "sensitive-provider-configuration" not in json.dumps(result)


def test_imported_inventory_output_is_bounded(resources):
    mod, factory = resources
    with factory() as session:
        for number in range(mod.MAX_VISIBLE_ASSETS + 1):
            session.add(
                mod.ExistingAsset(
                    id=f"import_{number}",
                    workspace_id="ws_a",
                    venture_id="ven_a",
                    kind="repo",
                    name="Imported repository",
                    source="import",
                    exists=True,
                    created_at=mod.utcnow(),
                )
            )
        session.commit()
    with pytest.raises(ValueError, match="readable asset limit"):
        mod.snapshot("ws_a", "ven_a")


def test_native_static_capability_is_independent_of_paused_paid_builder(resources, monkeypatch):
    mod, _ = resources
    monkeypatch.setattr('app.builder.budgets.runtime_quote', lambda _: {'ready': False, 'reasons': ['paused']})
    snapshot = mod.snapshot('ws_a', 'ven_a')
    capabilities = {item['id']: item for item in snapshot['capabilities']}
    if mod.is_local():
        assert capabilities['firstlook.landing']['state'] == 'unavailable'
        assert capabilities['firstlook.landing']['supported_milestone_kinds'] == ['landing']
        assert 'build.run' not in capabilities and 'coding.subscription_static' not in capabilities
        return
    native = capabilities['coding.subscription_static']
    assert native['state'] == 'implemented' and native['metered'] is False
    assert native['supported_milestone_kinds'] == ['landing']
    assert native['provider_configuration'] is None
    assert capabilities['build.run']['state'] == 'unavailable'
    assert 'independent QA before its own admission' in native['excluded_work']


@pytest.mark.hosted_only
def test_native_product_source_is_unavailable_until_whole_runtime_chain_exists(resources):
    mod, _ = resources
    rows = {row["id"]: row for row in mod.snapshot("ws_a", "ven_a")["capabilities"]}
    product = rows["coding.subscription_product_source"]
    assert product["state"] == "unavailable" and product["metered"] is False
    assert product["included_work"] == []
    assert product["supported_milestone_kinds"] == ["core"]
    assert "native execution" in product["excluded_work"]
    assert "import" in product["reason"] and "QA" in product["reason"]
    assert rows["coding.subscription_static"]["state"] == "implemented"


@pytest.mark.hosted_only
def test_future_product_source_cannot_become_a_native_executor_from_cli_presence(resources):
    mod, _ = resources
    from app.build.device_orders import CODING_CAPABILITIES
    from app.planning.compiler import _task_scope
    rows = {row["id"]: row for row in mod.snapshot("ws_a", "ven_a")["capabilities"]}
    task = {"capability_id": "coding.subscription_product_source", "implementation": "new_code"}
    assert task["capability_id"] not in CODING_CAPABILITIES
    assert _task_scope(task, rows, "core")[0] == "out"


def test_landing_is_always_the_codex_static_capability_never_firstlook(resources, monkeypatch):
    mod, _ = resources
    monkeypatch.setenv("SUPYMEM_LANDING_LANE", "firstlook")  # retired: the env is ignored
    rows = {row["id"]: row for row in mod.snapshot("ws_a", "ven_a")["capabilities"]}
    if mod.is_local():
        assert rows["firstlook.landing"]["state"] == "unavailable"
        assert "coding.subscription_static" not in rows
        return
    assert "firstlook.landing" not in rows and rows["coding.subscription_static"]["state"] == "implemented"

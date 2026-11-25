"""No live GitHub calls: exercise tenant selection and untrusted API responses."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from connectors.http import Reply
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.delenv("RESEARCH_GITHUB_TOKEN", raising=False)
    # These ambient credentials must never be used by public research.
    monkeypatch.setenv("GITHUB_TOKEN", "forbidden-github-token")
    monkeypatch.setenv("GH_TOKEN", "forbidden-cli-token")
    load_company_os_app()
    from app.connectors.secrets import LocalSecretStore
    from app.research import github_search

    engine = create_engine(f"sqlite:///{tmp_path}/connections.db")
    github_search.Connection.__table__.create(engine)
    factory = sessionmaker(engine)
    vault = LocalSecretStore()
    monkeypatch.setattr(github_search, "db", lambda: factory)
    monkeypatch.setattr(github_search, "store", lambda: vault)
    calls = []

    async def request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return Reply(200, {"items": [repo()], "incomplete_results": False})

    monkeypatch.setattr(github_search, "request_json", request)

    def connection(identifier="selected", *, workspace_id="workspace-a", **overrides):
        token = f"test-token-{identifier}"
        ref = vault.put(
            workspace_id=workspace_id, provider="github", key="token", value=token
        )
        values = {
            "id": identifier,
            "workspace_id": workspace_id,
            "provider": "github",
            "external_account": identifier,
            "status": "active",
            "simulated": 0,
            "secret_ref": ref,
            "scopes_json": json.dumps([{"scope": "metadata:read"}]),
            "created_at": datetime.now(timezone.utc),
        }
        values.update(overrides)
        with factory() as session:
            session.add(github_search.Connection(**values))
            session.commit()
        return ref

    yield github_search, calls, connection, vault
    engine.dispose()


def repo(**overrides):
    value = {
        "full_name": "example/public-repo",
        "html_url": "https://github.com/example/public-repo",
        "private": False,
        "visibility": "public",
        "description": "Public repository description",
        "stargazers_count": 42,
        "pushed_at": "2026-09-01T12:00:00Z",
        "archived": False,
        "license": {"spdx_id": "MIT", "url": "https://api.github.com/licenses/mit"},
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_anonymous_public_fixed_endpoint_and_metadata(adapter):
    mod, calls, _, _ = adapter
    result = await mod.search_public_repositories("vector memory", "workspace-a")
    assert result["credential_source"] == "anonymous"
    assert result["provider"] == "github"
    assert result["incomplete"] is False
    assert result["items"][0]["repository"] == {
        "full_name": "example/public-repo",
        "private": False,
        "stars": 42,
        "pushed_at": "2026-09-01T12:00:00Z",
        "archived": False,
        "license_spdx": "MIT",
        "license_url": "https://api.github.com/licenses/mit",
    }
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://api.github.com/search/repositories"
    assert calls[0]["params"] == {
        "q": "vector memory is:public",
        "per_page": 5,
        "page": 1,
    }
    assert calls[0]["headers"] == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": mod.API_VERSION,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse stock forecasting", "senior dog exercise"])
@pytest.mark.parametrize("selection", ["anonymous", "selected", "revoked", "declined", "foreign", "missing"])
async def test_local_repository_credentials_use_only_eligible_saved_connection(adapter, monkeypatch, query, selection):
    mod, calls, connection, vault = adapter
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("RESEARCH_GITHUB_TOKEN", "forbidden-local-environment-token")
    if selection != "anonymous":
        overrides = {
            "revoked": {"revoked_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            "declined": {"declined_scopes_json": '["metadata:read"]'},
            "foreign": {"workspace_id": "another-tenant"},
        }.get(selection, {})
        reference = connection(**overrides)
        if selection == "missing":
            vault.forget(reference)
    if selection == "missing":
        with pytest.raises(mod.ConnectorError, match="Keys and connections"):
            await mod.search_public_repositories(query, "workspace-a")
        assert not calls
        return
    result = await mod.search_public_repositories(query, "workspace-a")
    assert result["credential_source"] == ("workspace" if selection == "selected" else "anonymous")
    assert calls[0]["headers"].get("Authorization") == ("Bearer test-token-selected" if selection == "selected" else None)
    assert "token" not in json.dumps(result)


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_workspace_credential_preferred_over_platform(adapter, monkeypatch):
    mod, calls, connection, _ = adapter
    connection(scopes_json='["metadata:read"]')
    monkeypatch.setenv("RESEARCH_GITHUB_TOKEN", "platform-token")
    result = await mod.search_public_repositories("search", "workspace-a")
    assert result["credential_source"] == "workspace"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token-selected"
    assert "test-token" not in json.dumps(result)


@pytest.mark.hosted_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": "another-tenant"},
        {"simulated": 1},
        {"provider": "linear"},
        {"status": "revoked"},
        {"status": "pending"},
        {"scopes_json": '["contents:read"]'},
        {"scopes_json": "malformed"},
        {"declined_scopes_json": '["metadata:read"]'},
        {"revoked_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ],
)
async def test_ineligible_connections_never_supply_credentials(
    adapter, monkeypatch, overrides
):
    mod, calls, connection, _ = adapter
    connection(**overrides)
    monkeypatch.setenv("RESEARCH_GITHUB_TOKEN", "platform-token")
    result = await mod.search_public_repositories("search", "workspace-a")
    assert result["credential_source"] == "platform"
    assert calls[0]["headers"]["Authorization"] == "Bearer platform-token"


@pytest.mark.hosted_only
@pytest.mark.asyncio
@pytest.mark.parametrize("secret", ["forgotten", None, "plaintext:unsafe", "empty"])
async def test_missing_selected_workspace_key_refuses_fallback(
    adapter, monkeypatch, secret
):
    mod, calls, connection, vault = adapter
    monkeypatch.setenv("RESEARCH_GITHUB_TOKEN", "platform-token")
    if secret in {"forgotten", "empty"}:
        ref = connection()
        if secret == "forgotten":
            vault.forget(ref)
        else:
            vault._values[ref] = ""
    else:
        connection(secret_ref=secret)
    with pytest.raises(mod.ConnectorError, match="workspace credential is unavailable"):
        await mod.search_public_repositories("search", "workspace-a")
    assert calls == []


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_vault_errors_do_not_disclose_secret_or_ref(adapter, monkeypatch):
    mod, calls, connection, vault = adapter
    connection()

    def unavailable(*args, **kwargs):
        raise PermissionError("sensitive-vault-message")

    monkeypatch.setattr(vault, "get", unavailable)
    with pytest.raises(mod.ConnectorError) as error:
        await mod.search_public_repositories("search", "workspace-a")
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"private": True},
        {"private": None},
        {"private": "false"},
        {"private": 0},
        {"visibility": "internal"},
        {"full_name": "../repo"},
        {"full_name": "example/.."},
        {"full_name": "example/repo/extra"},
        {"full_name": "example/" + "a" * 101},
        {"html_url": "https://evil.example/example/public-repo"},
        {"html_url": "https://github.com@evil.example/example/public-repo"},
        {"html_url": "https://github.com/example/public-repo?token=secret"},
        {"html_url": "https://github.com/example/public-repo#redirect"},
        {"html_url": "https://github.com:443/example/public-repo"},
        {"html_url": "http://github.com/example/public-repo"},
        {"html_url": "javascript:alert(1)"},
    ],
)
async def test_private_or_malicious_results_are_discarded(adapter, monkeypatch, patch):
    mod, _, _, _ = adapter

    async def request(*args, **kwargs):
        return Reply(200, {"items": [repo(**patch), repo()]})

    monkeypatch.setattr(mod, "request_json", request)
    result = await mod.search_public_repositories("search", "workspace-a")
    assert len(result["items"]) == 1
    assert result["items"][0]["repository"]["private"] is False
    assert result["incomplete"] is True


@pytest.mark.asyncio
async def test_optional_metadata_is_validated_without_fabrication(adapter, monkeypatch):
    mod, _, _, _ = adapter

    async def request(*args, **kwargs):
        return Reply(
            200,
            {
                "items": [
                    repo(
                        stargazers_count=True,
                        archived="false",
                        pushed_at="yesterday",
                        license={
                            "spdx_id": "MIT\n<script>",
                            "url": "https://evil.example/license",
                        },
                        description="hello\x00\nworld " + "x" * 10000,
                    )
                ]
            },
        )

    monkeypatch.setattr(mod, "request_json", request)
    result = await mod.search_public_repositories("search", "workspace-a")
    item = result["items"][0]
    assert item["snippet"].startswith("hello world ")
    assert len(item["snippet"]) == 2000
    for key in ("stars", "archived", "pushed_at", "license_spdx", "license_url"):
        assert item["repository"][key] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "",
        " ",
        "x" * 241,
        "x\n",
        "x\x00",
        "is:private",
        "is:internal",
        "-is:public",
        "visibility:private",
    ],
)
async def test_invalid_query_rejected_before_network(adapter, query):
    mod, calls, _, _ = adapter
    with pytest.raises(ValueError):
        await mod.search_public_repositories(query, "workspace-a")
    assert calls == []


@pytest.mark.asyncio
async def test_query_is_only_a_parameter_and_results_are_bounded(adapter, monkeypatch):
    mod, calls, _, _ = adapter
    query = "https://evil.example/$(command)?x=1&per_page=100 #fragment"
    result = await mod.search_public_repositories(query, "workspace-a", limit=500)
    assert len(result["items"]) == 1
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.github.com/search/repositories"
    assert calls[0]["params"] == {"q": query + " is:public", "per_page": 10, "page": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers,body,error_name",
    [
        (429, {"Retry-After": "2"}, {}, "RateLimited"),
        (403, {"x-ratelimit-remaining": "0"}, {}, "RateLimited"),
        (403, {"retry-after": "3"}, {}, "RateLimited"),
        (
            403,
            {},
            {"message": "You have exceeded a secondary rate limit"},
            "RateLimited",
        ),
        (401, {}, {"message": "sensitive-body"}, "AuthExpired"),
        (403, {}, {"message": "sensitive-body"}, "AuthExpired"),
        (422, {}, {"message": "sensitive-body"}, "ConnectorError"),
        (500, {}, {"message": "sensitive-body"}, "ConnectorError"),
        (302, {"location": "https://evil.example"}, {}, "ConnectorError"),
    ],
)
async def test_provider_errors_are_explicit_safe_and_not_retried(
    adapter, monkeypatch, status, headers, body, error_name
):
    mod, calls, _, _ = adapter

    async def request(*args, **kwargs):
        calls.append(kwargs)
        return Reply(status, body, headers)

    monkeypatch.setattr(mod, "request_json", request)
    with pytest.raises(getattr(mod, error_name)) as error:
        await mod.search_public_repositories("search", "workspace-a")
    assert "sensitive-body" not in str(error.value)
    assert len(calls) == 1
    if error_name == "RateLimited":
        assert error.value.provider == "github"
        assert error.value.retry_after_s >= 0


@pytest.mark.asyncio
async def test_transport_failure_is_sanitized_and_not_retried(adapter, monkeypatch):
    mod, calls, _, _ = adapter

    async def request(*args, **kwargs):
        calls.append(kwargs)
        raise httpx.ConnectError("sensitive-request-data")

    monkeypatch.setattr(mod, "request_json", request)
    with pytest.raises(
        mod.ConnectorError, match="github search request failed"
    ) as error:
        await mod.search_public_repositories("search", "workspace-a")
    assert "sensitive" not in str(error.value)
    assert error.value.__suppress_context__
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, [], "sensitive-body", {}, {"items": {}}])
async def test_malformed_response_fails_clearly(adapter, monkeypatch, body):
    mod, _, _, _ = adapter

    async def request(*args, **kwargs):
        return Reply(200, body)

    monkeypatch.setattr(mod, "request_json", request)
    with pytest.raises(mod.ConnectorError, match="invalid response"):
        await mod.search_public_repositories("search", "workspace-a")


@pytest.mark.asyncio
async def test_response_rows_and_pagination_are_bounded(adapter, monkeypatch):
    mod, calls, _, _ = adapter

    async def request(*args, **kwargs):
        calls.append(kwargs)
        rows = [
            repo(
                full_name=f"example/repo-{i}",
                html_url=f"https://github.com/example/repo-{i}",
            )
            for i in range(25)
        ]
        return Reply(
            200,
            {"items": rows, "incomplete_results": True},
            {"link": '<https://evil.example>; rel="next"'},
        )

    monkeypatch.setattr(mod, "request_json", request)
    result = await mod.search_public_repositories("search", "workspace-a", limit=2)
    assert len(result["items"]) == 2
    assert result["incomplete"] is True
    assert len(calls) == 1

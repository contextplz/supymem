"""The new GTM agent shares the actual workspace credit/provider boundary."""
from tests.company_os_harness import load_company_os_app


import pytest

pytestmark = pytest.mark.hosted_only


def test_god_json_uses_workspace_meter_and_its_own_schema(monkeypatch, tmp_path):
    load_company_os_app()
    from tests.unit.gtm.test_draft_gateway import _fresh_db, _completion
    _fresh_db(monkeypatch, tmp_path)
    from app.credits.accounts import get_account_by_id
    from app.gtm.god.llm import json_chat
    from llm_gateway.boundary import Decision, register_filter
    from llm_gateway import sole
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setenv("SANDBOX_MODE", "false")
    calls = []
    requests = []
    transmit = sole.transmit
    async def capture_request(*args, **kwargs):
        requests.append(kwargs)
        return await transmit(*args, **kwargs)
    monkeypatch.setattr(sole, "transmit", capture_request)
    async def primary(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        return _completion('{"bottleneck":"Interview evidence is missing"}')
    register_filter(lambda messages, **_: Decision(messages=list(messages)))
    sole.set_transmit_hooks(primary=primary)
    try:
        result = json_chat(purpose="gtm.god_diagnose", system="Use evidence", user="Test idea",
                           workspace_id="ws_draft")
    finally:
        sole.set_transmit_hooks(primary=None, fallback=None)
        register_filter(None)
    assert result == {"bottleneck": "Interview evidence is missing"}
    assert len(calls) == 1
    assert calls[0]["workspace_id"] == "ws_draft"
    assert calls[0]["purpose"] == "gtm.god_diagnose"
    assert requests[0]["response_schema"] == {"type": "object"}
    assert requests[0]["single_attempt"] is True
    assert calls[0]["messages"][0]["content"] == "Use evidence"
    account = get_account_by_id("cra_draft")
    assert account.spent_micro > 0 and account.reserved_micro == 0


def test_god_without_workspace_never_calls_provider(monkeypatch):
    load_company_os_app()
    from app.gtm.god.llm import json_chat
    from app.gtm.supymem import draft
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setenv("SANDBOX_MODE", "false")
    def forbidden(*args, **kwargs):
        raise AssertionError("unscoped provider call")
    monkeypatch.setattr(draft, "_canonical_complete", forbidden)
    assert json_chat(purpose="gtm.god_diagnose", system="x", user="y") is None


def test_god_uncertain_provider_outcome_keeps_credit_hold(monkeypatch, tmp_path):
    load_company_os_app()
    from tests.unit.gtm.test_draft_gateway import _fresh_db
    _fresh_db(monkeypatch, tmp_path)
    from app.credits.accounts import get_account_by_id
    from app.gtm.god.llm import json_chat
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setenv("SANDBOX_MODE", "false")
    calls = []
    async def timed_out(*args, **kwargs):
        calls.append(kwargs)
        raise ProviderCallError("provider_down", "timeout", retryable=True)
    monkeypatch.setattr(sole, "transmit", timed_out)
    assert json_chat(purpose="gtm.god_diagnose", system="x", user="y", workspace_id="ws_draft") is None
    assert len(calls) == 1
    account = get_account_by_id("cra_draft")
    assert account.spent_micro == 0 and account.reserved_micro > 0

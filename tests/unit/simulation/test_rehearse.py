"""Production queue/journal/holds with an offline provider transport only."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from tests.company_os_harness import load_company_os_app


def seed(d):
    from app.core.local_mode import is_local
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, VentureDocument
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm.journey import _draft_revision
    from core.clock import utcnow
    now = utcnow()
    with d["factory"]() as session:
        if not session.get(IdeaSession, d["sid"]):
            session.add(IdeaSession(id=d["sid"], workspace_id=d["wid"], venture_id=d["vid"], case="know", state="confirmed",
                current_revision_id=d["rid"], confirmed_revision_id=d["rid"], created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id=d["rid"], session_id=d["sid"], workspace_id=d["wid"], venture_id=d["vid"],
                rev=1, source="founder", summary="A board for bakery orders", created_at=now))
        session.add(VentureDocument(id="icp_" + d["vid"], venture_id=d["vid"], kind="gtm_journey", title="Audience",
            provenance_json=json.dumps({"icp": {"who": "Independent bakery owners handling phone orders", "confirmed": True}}), created_at=now, updated_at=now))
        refs = []
        for i in range(2):
            did = f"draft{i}_{d['vid']}"
            data = {"doc_id": did, "subject": f"A question about orders {i}", "body": f"How do you track bakery orders today? Variant {i}",
                "to": "private@example.test", "contact_id": "private-contact", "source_url": "https://private.example.test",
                "state": "draft", "review_state": "draft", "batch_id": "batch", "day": 1}
            refs.append({"doc_id": did, "revision": _draft_revision(data)})
            session.add(VentureDocument(id=did, venture_id=d["vid"], kind="outreach_draft", title="Draft", body_md=data["body"],
                provenance_json=json.dumps({k: v for k, v in data.items() if k != "doc_id"}), created_at=now, updated_at=now))
        session.commit()
    if is_local():
        d.update(refs=refs, account=None)
        return d
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with d["factory"]() as session:
        session.get(CreditAccount, account.id).granted_micro = 10_000_000
        session.commit()
    d.update(refs=refs, account=account.id)
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/simulation.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SUPYMEM_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("LLM_API_KEY", "offline-fixture-key")
    load_company_os_app()
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings
        from app.core.providers_local import save_settings

        save_settings(provider="openai", api_key="offline-fixture-key", models={"chat": "gpt-6.1-sol"})
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    from app.simulation import service
    # Keep five-minute quote boundary fixtures deterministic, without changing worker time.
    import time
    fixed_time = int(time.time()) // 300 * 300 + 1
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: fixed_time))
    from app.authz.workspace import ensure_subject_person
    from app.db.models import Venture
    from app.state import db
    from app.workspaces.service import create_workspace
    from core.clock import utcnow
    actor = "usr_simulation"
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Reader check fixture", owner_id=actor)
    with db()() as session:
        session.add(Venture(id="ven_simulation", workspace_id=workspace.id, name="Order board", raw_idea="Bakery board",
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    return seed({"factory": db(), "wid": workspace.id, "vid": "ven_simulation", "actor": actor, "sid": "session_simulation", "rid": "revision_simulation"})


def start(d, key="one"):
    from app.simulation import service
    q = service.quote(d["wid"], d["vid"], d["actor"], d["refs"])
    result = service.start(d["wid"], d["vid"], d["actor"], d["refs"], epoch=q["epoch"], quote_digest=q["quote_digest"], confirmed=True, request_id=key)
    return result, q


def lease(d, jid):
    from app.core.jobs import lease
    from app.simulation import service
    with d["factory"]() as session:
        jobs = lease(session, "reader-fixture", kinds=[service.KIND], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].id == jid
    job = jobs[0]
    return job.payload | {"_job_id": job.id, "_worker_id": "reader-fixture", "_job_attempt": job.attempts}


def install_transport(monkeypatch, *, fail=None, after_call=None):
    from llm_gateway import sole
    calls = []
    async def transmit(**kwargs):
        assert kwargs["single_attempt"] is True and kwargs["use_workspace_override"] is False
        assert kwargs["purpose"] == "simulation.rehearse" and kwargs["max_tokens"] == 4096
        task = json.loads(kwargs["messages"][1]["content"])
        assert "private@example.test" not in json.dumps(task) and "private-contact" not in json.dumps(task)
        calls.append(kwargs)
        if fail == "timeout":
            raise TimeoutError("private provider diagnostic must not escape")
        if "audience" in task:
            answer = {"personas": [{"id": f"p{i}", "role": f"Bakery owner {i}", "situation": f"Hypothetical busy morning {i}"} for i in range(1, 9)]}
            if fail == "duplicate_panel":
                answer["personas"][-1]["id"] = "p1"
        elif "presentations" in task:
            answer = {"reactions": [{"persona_id": item["persona"]["id"], "variant_id": v["id"], "clarity": 3,
                "confusing_phrase": "", "objection": "How would this fit my existing order book?"} for item in task["presentations"] for v in item["variants"]]}
            if fail == "foreign_variant":
                answer["reactions"][0]["variant_id"] = "v3"
            if fail == "invented_quote":
                answer["reactions"][0]["confusing_phrase"] = "a quotation not present in the saved draft"
        else:
            answer = {"clarity_order": [v["id"] for v in task["variants"]], "explanation": "Relative clarity in this synthetic panel only.",
                "top_objections": ["Workflow fit is unclear"], "confusing_phrases": [], "suggested_edit": "How do you track bakery orders today?",
                "interview_questions": ["Tell me about your last lost order.", "What do you use to track orders?"]}
        content = "not json" if fail == "invalid" else json.dumps(answer)
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length" if fail == "truncated" else "stop", message=SimpleNamespace(content=content))],
            usage=None if fail == "usage" else {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        if after_call:
            after_call(len(calls))
        return response
    monkeypatch.setattr(sole, "transmit", transmit)
    return calls


@pytest.mark.hosted_only
def test_real_credit_four_batched_calls_saved_labels_and_idempotence(env, monkeypatch):
    from app.core.jobs import complete_job
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, GtmLearningEvent, GtmTouch, VentureDocument
    from app.simulation import jobs, service
    d = env
    view, quote = start(d)
    with d["factory"]() as session:
        before = [(x.id, x.body_md, x.provenance_json) for x in session.scalars(select(VentureDocument))]
    calls = install_transport(monkeypatch)
    payload = lease(d, view["job_id"])
    result = asyncio.run(jobs.handle(payload))
    assert result["badge"] == "Simulated · AI personas" and "Not customer evidence" in result["disclaimer"]
    assert result["persona_count"] == 8 and len(result["reactions"]) == 16 and len(calls) == 4
    with d["factory"]() as session:
        complete_job(session, view["job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.purpose == "simulation.rehearse")))
        assert len(holds) == 4 and all(h.status == "settled" and h.request_id.startswith(view["job_id"] + ":") for h in holds)
        assert session.get(CreditAccount, d["account"]).reserved_micro == 0
        assert session.scalar(select(CreditLedgerEntry.id).where(CreditLedgerEntry.account_id == d["account"]))
        assert [(x.id, x.body_md, x.provenance_json) for x in session.scalars(select(VentureDocument))] == before
        assert session.scalar(select(func.count()).select_from(GtmLearningEvent)) == 0
        assert session.scalar(select(func.count()).select_from(GtmTouch)) == 0
    saved = service.status(d["wid"], d["vid"], d["actor"], view["job_id"])
    assert saved["state"] == "completed" and 0 < saved["credits_micro"] <= quote["resources"]["ceiling_micro"]
    assert start(d)[0]["job_id"] == view["job_id"] and len(calls) == 4
    assert start(d, "another-click")[0]["job_id"] == view["job_id"] and len(calls) == 4
    assert service.quote(d["wid"], d["vid"], d["actor"], d["refs"])["existing_job"]["job_id"] == view["job_id"]


@pytest.mark.hosted_only
@pytest.mark.parametrize("failure,held,code", [("usage", True, "outcome_uncertain"), ("timeout", True, "outcome_uncertain"),
    ("invalid", False, "invalid_output"), ("truncated", False, "output_truncated")])
def test_real_holds_unknown_or_known_failure_never_redials(env, monkeypatch, failure, held, code):
    from app.core.jobs import FatalError
    from app.db.models import CreditAccount, CreditReservation
    from app.simulation import runtime
    d = env
    view, _ = start(d)
    calls = install_transport(monkeypatch, fail=failure)
    payload = lease(d, view["job_id"])
    for _ in range(2):
        with pytest.raises(FatalError, match=code):
            runtime.run(payload)
    assert len(calls) == 1
    assert start(d, "new-key")[0]["job_id"] == view["job_id"]
    with d["factory"]() as session:
        hold = session.scalar(select(CreditReservation).where(CreditReservation.purpose == "simulation.rehearse"))
        assert hold.status == ("reconciling" if held else "settled")
        assert (session.get(CreditAccount, d["account"]).reserved_micro > 0) is held


@pytest.mark.hosted_only
def test_settlement_uncertainty_stops_later_calls_and_preserves_receipt(env, monkeypatch):
    from app.core.jobs import FatalError
    from app.credits import gate, reserve
    from app.db.models import CreditAccount
    from app.simulation import runtime
    d = env
    view, _ = start(d)
    calls = install_transport(monkeypatch)
    original = reserve.settle
    def broken(*args, **kwargs):
        raise RuntimeError("offline settlement write failure")
    monkeypatch.setattr(reserve, "settle", broken)
    monkeypatch.setattr(gate, "settle", broken)
    payload = lease(d, view["job_id"])
    with pytest.raises(FatalError, match="settlement_pending"):
        runtime.run(payload)
    assert len(calls) == 1
    op = runtime.document(view["job_id"])["_runtime"]["operations"]["panel"]
    assert op["state"] == "responded" and op["usage"]["provenance"] == "provider_usage"
    with d["factory"]() as session:
        assert session.get(CreditAccount, d["account"]).reserved_micro > 0
    monkeypatch.setattr(reserve, "settle", original)
    monkeypatch.setattr(gate, "settle", original)
    runtime.JOURNAL.recover(view["job_id"], "panel")
    assert len(calls) == 1 and runtime.document(view["job_id"])["_runtime"]["operations"]["panel"]["usage"]["settled"]


@pytest.mark.parametrize("failure,count", [("duplicate_panel", 1), ("foreign_variant", 2), ("invented_quote", 2)])
def test_fabricated_or_foreign_reactions_are_charged_and_not_applied(env, monkeypatch, failure, count):
    from app.core.jobs import FatalError
    from app.simulation import runtime
    view, _ = start(env)
    calls = install_transport(monkeypatch, fail=failure)
    with pytest.raises(FatalError, match="invalid_output"):
        runtime.run(lease(env, view["job_id"]))
    assert len(calls) == count and not runtime.document(view["job_id"])["_runtime"].get("result")
    assert all(op["usage"]["settled"] for op in runtime.document(view["job_id"])["_runtime"]["operations"].values())


def test_byok_retains_real_usage_without_platform_holds(env, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.models import CreditReservation
    from app.llm import founder_provider
    from app.simulation import runtime
    from llm_gateway.provider_foundry import load_provider_config_from_env
    if not is_local():
        config = load_provider_config_from_env().model_copy(deep=True)
        resolved = founder_provider.ResolvedLLMConfig(workspace_id=env["wid"], provider=config.provider,
            base_url=config.base_url, model=config.deployment_pro, api_key_ref="offline-key-reference", source="founder_override")
        monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
        monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: config)
    view, quote = start(env)
    assert quote["resources"]["ceiling_micro"] == 0
    calls = install_transport(monkeypatch)
    runtime.run(lease(env, view["job_id"]))
    assert len(calls) == 4
    ops = runtime.document(view["job_id"])["_runtime"]["operations"]
    assert all(op["usage"]["source"] == "byok" and op["usage"]["provenance"] == "provider_usage" and op["usage"]["credits_micro"] == 0 for op in ops.values())
    with env["factory"]() as session:
        assert not session.scalar(select(CreditReservation.id).where(CreditReservation.purpose == "simulation.rehearse"))


def test_changed_drafts_cannot_repeat_an_unresolved_provider_request(env, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.models import VentureDocument
    from app.gtm.journey import _draft_revision
    from app.simulation import runtime
    view, _ = start(env)
    calls = install_transport(monkeypatch, fail="usage")
    with pytest.raises(FatalError, match="outcome_uncertain"):
        runtime.run(lease(env, view["job_id"]))
    with env["factory"]() as session:
        row = session.get(VentureDocument, env["refs"][0]["doc_id"])
        data = json.loads(row.provenance_json) | {"doc_id": row.id, "body": "Changed message"}
        row.body_md = data["body"]
        row.provenance_json = json.dumps({k: v for k, v in data.items() if k != "doc_id"})
        env["refs"][0]["revision"] = _draft_revision(data)
        session.commit()
    with pytest.raises(ValueError, match="previous_outcome_unresolved"):
        start(env, "changed-draft-click")
    assert len(calls) == 1


@pytest.mark.hosted_only
@pytest.mark.parametrize("change", ["draft", "revision", "archived", "revoked", "flag"])
def test_midflight_retirement_retains_charged_receipt_without_later_call_or_result(env, monkeypatch, change):
    from app.core.jobs import FatalError
    from app.db.models import Venture, VentureDocument, Workspace, WorkspaceMember
    from app.db.supymem_models import IdeaSession
    from app.simulation import runtime
    from core.clock import utcnow
    d = env
    if change == "revoked":
        from app.authz.workspace import ensure_subject_person
        ensure_subject_person("usr_another_owner")
    view, _ = start(d)
    def retire(n):
        if n != 1:
            return
        if change == "flag":
            monkeypatch.setenv("SUPYMEM_SIMULATION_ENABLED", "false")
        else:
            with d["factory"]() as session:
                if change == "draft":
                    session.get(VentureDocument, d["refs"][0]["doc_id"]).body_md = "New draft"
                if change == "revision":
                    session.get(IdeaSession, d["sid"]).current_revision_id = "other"
                if change == "archived":
                    session.get(Venture, d["vid"]).archived_at = utcnow()
                if change == "revoked":
                    session.get(Workspace, d["wid"]).owner_id = "usr_another_owner"
                    member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"], WorkspaceMember.person_id == d["actor"]))
                    if member:
                        session.delete(member)
                session.commit()
    calls = install_transport(monkeypatch, after_call=retire)
    with pytest.raises(FatalError, match="scope_changed"):
        runtime.run(lease(d, view["job_id"]))
    assert len(calls) == 1 and not runtime.document(view["job_id"])["_runtime"].get("result")
    assert runtime.document(view["job_id"])["_runtime"]["operations"]["panel"]["usage"]["settled"]
    if change == "revoked":
        from app.db.models import CreditAccount, CreditReservation
        with d["factory"]() as session:
            wallet = session.get(CreditAccount, d["account"])
            assert wallet.spent_micro > 0 and wallet.reserved_micro == 0
            holds = list(session.scalars(select(CreditReservation).where(CreditReservation.purpose == "simulation.rehearse")))
            assert len(holds) == 1 and holds[0].account_id == wallet.id and holds[0].status == "settled"
            assert not session.scalar(select(CreditAccount.id).where(CreditAccount.owner_person_id == "usr_another_owner"))


def test_quote_changed_expired_unconfirmed_and_foreign_are_not_enqueued(env, monkeypatch):
    from app.db.models import Job
    from app.simulation import service
    d = env
    q = service.quote(d["wid"], d["vid"], d["actor"], d["refs"])
    for kwargs, error in [({"confirmed": False}, "confirmation_required"), ({"epoch": q["epoch"] - 1}, "quote_expired"),
        ({"quote_digest": "0" * 64}, "quote_changed")]:
        base = dict(epoch=q["epoch"], quote_digest=q["quote_digest"], confirmed=True, request_id="bad") | kwargs
        with pytest.raises(ValueError, match=error):
            service.start(d["wid"], d["vid"], d["actor"], d["refs"], **base)
    with pytest.raises(KeyError):
        service.quote(d["wid"], d["vid"], "foreign", d["refs"])
    with d["factory"]() as session:
        assert not session.scalar(select(Job.id).where(Job.kind == service.KIND))


def test_deadline_after_known_response_stops_new_call_and_application(env, monkeypatch):
    from app.core.jobs import FatalError
    from app.simulation import runtime
    d = env
    view, _ = start(d)
    clock = runtime.time
    def expire(n):
        if n == 1:
            monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: clock.time() + 151))
    calls = install_transport(monkeypatch, after_call=expire)
    with pytest.raises(FatalError, match="simulation_deadline"):
        runtime.run(lease(d, view["job_id"]))
    assert len(calls) == 1
    doc = runtime.document(view["job_id"])
    assert not doc["_runtime"].get("result") and doc["_runtime"]["operations"]["panel"]["usage"]["settled"]


def test_authorized_collaborator_cannot_repeat_another_actors_unknown_request(env, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.core.jobs import FatalError
    from app.db.models import WorkspaceMember
    from app.simulation import runtime, service
    from core.clock import utcnow
    d = env
    view, _ = start(d)
    calls = install_transport(monkeypatch, fail="usage")
    with pytest.raises(FatalError, match="outcome_uncertain"):
        runtime.run(lease(d, view["job_id"]))
    ensure_subject_person("usr_collaborator")
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="member_simulation", workspace_id=d["wid"], person_id="usr_collaborator", role="admin", created_at=utcnow()))
        session.commit()
    quote = service.quote(d["wid"], d["vid"], "usr_collaborator", d["refs"])
    assert quote["existing_job"] is None
    with pytest.raises(ValueError, match="previous_outcome_unresolved"):
        service.start(d["wid"], d["vid"], "usr_collaborator", d["refs"], epoch=quote["epoch"], quote_digest=quote["quote_digest"], confirmed=True, request_id="collaborator-click")
    with pytest.raises(KeyError):
        service.status(d["wid"], d["vid"], "usr_collaborator", view["job_id"])
    assert len(calls) == 1


def test_current_saved_result_uses_frozen_quote_without_fresh_provider_lookup(env, monkeypatch):
    from app.simulation import runtime, service
    view, quote = start(env)
    calls = install_transport(monkeypatch)
    runtime.run(lease(env, view["job_id"]))
    def unavailable(_workspace):
        raise AssertionError("Saved result recovery must not acquire current provider resources")
    monkeypatch.setattr(service, "resources", unavailable)
    restored = service.quote(env["wid"], env["vid"], env["actor"], env["refs"], epoch=quote["epoch"] - 1)
    assert restored["quote_digest"] == quote["quote_digest"] and restored["resources"] == quote["resources"]
    assert restored["epoch"] == quote["epoch"] and restored["existing_job"]["job_id"] == view["job_id"]
    assert restored["existing_job"]["state"] == "completed" and len(calls) == 4


@pytest.mark.hosted_only
def test_duplicate_worker_delivery_preserves_active_leased_attempt_and_all_settled_results(env, monkeypatch):
    from threading import Event
    from app.core.jobs import lease as claim
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.simulation import service
    from app.worker import handlers
    from app.worker.main import _run_one
    from llm_gateway import sole
    queued, _ = start(env)
    calls = install_transport(monkeypatch)
    transport = sole.transmit
    entered, release = Event(), Event()

    async def held_transport(**kwargs):
        if not entered.is_set():
            entered.set()
            if not await asyncio.to_thread(release.wait, 10):
                raise TimeoutError("Offline duplicate-delivery barrier expired")
        return await transport(**kwargs)

    monkeypatch.setattr(sole, "transmit", held_transport)
    handlers.install_default_handlers()
    with env["factory"]() as session:
        claimed = claim(session, "duplicate-reader-fixture", kinds=[service.KIND], n=1)[0]
        session.commit()
    assert claimed.id == queued["job_id"]

    async def exercise():
        running = {(claimed.id, claimed.attempts)}
        active = asyncio.create_task(_run_one(env["factory"], claimed, running))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            await _run_one(env["factory"], claimed, running)
            with env["factory"]() as session:
                row = session.get(Job, claimed.id)
                assert row.status == "leased" and row.attempts == 1
                assert row.last_error is None and (claimed.id, claimed.attempts) in running
                operations = json.loads(row.payload_json)["_runtime"]["operations"]
                assert operations["panel"]["state"] == "calling"
                assert session.get(CreditAccount, env["account"]).reserved_micro > 0
            saved = service.status(env["wid"], env["vid"], env["actor"], claimed.id)
            assert saved["state"] == "running" and saved["error_code"] is None
        finally:
            release.set()
            await active
        assert not running

    asyncio.run(exercise())
    saved = service.status(env["wid"], env["vid"], env["actor"], claimed.id)
    assert saved["state"] == "completed" and saved["error_code"] is None
    assert len(calls) == 4 and saved["result"]["model_call_count"] == 4
    with env["factory"]() as session:
        row = session.get(Job, claimed.id)
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.purpose == service.KIND)))
        assert row.status == "succeeded" and row.attempts == 1 and row.last_error is None
        assert len(holds) == 4 and all(hold.status == "settled" for hold in holds)
        assert session.get(CreditAccount, env["account"]).reserved_micro == 0

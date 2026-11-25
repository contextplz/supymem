"""Persisted Firstlook intent and current scope; no provider or wallet mocks."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/admission.db")
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    load_company_os_app()
    from app import state
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.firstlook import admission
    from app.onboarding.resume import idempotency_key
    from app.onboarding.state import Ctx, create_run, record_running
    from app.tenancy import bind
    from core.clock import utcnow

    state.reset_db_factory()
    now = utcnow()
    with state.db()() as session:
        session.add(Person(id="person_firstlook", display_name="Founder", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_firstlook", kind="venture", name="Bakery", entry_mode="idea",
            owner_id="person_firstlook", settings_json="{}", created_at=now, updated_at=now))
        session.flush()
        session.add(WorkspaceMember(id="wm_firstlook", workspace_id="ws_firstlook",
            person_id="person_firstlook", role="owner", created_at=now))
        session.add(Venture(id="ven_firstlook", workspace_id="ws_firstlook", name="Bakery",
            raw_idea="A bakery order board", stage="draft", created_at=now, updated_at=now))
        session.commit()
    with bind(workspace_id="ws_firstlook", venture_id="ven_firstlook", actor="person_firstlook"):
        run = create_run("ven_firstlook", workspace_id="ws_firstlook")
        with state.db()() as session:
            session.get(type(run), run.id).phase = "commit"
            session.commit()
        run.phase = "commit"
        ctx = Ctx.build(run)
        key = idempotency_key(run.id, "commit.lock_charter", ctx.input_digest("commit.lock_charter"))
        step = record_running(run.id, "commit.lock_charter", "commit", key)
        intent = admission.capture_onboarding_intent(ctx)
        yield SimpleNamespace(module=admission, ctx=ctx, intent=intent, step=step, key=key,
            db=state.db, bind=bind, now=now)


def _validate(source, **kwargs):
    return source.module.validate_generation_intent(source.intent,
        workspace_id="ws_firstlook", venture_id="ven_firstlook", **kwargs)


def test_original_committed_key_has_distinct_slots_and_read_only_authority(admitted):
    from app.db.models import OnboardingStep
    source = admitted
    assert _validate(source) == source.intent
    calls = [source.module.generation_call_id(source.intent, workspace_id="ws_firstlook",
        venture_id="ven_firstlook", slot=slot) for slot in ("first", "lint-regenerate")]
    assert calls[0] != calls[1] and all(source.key in call for call in calls)
    assert all(source.step.id not in call for call in calls)
    with source.db()() as session:
        rows = session.query(OnboardingStep).all()
        assert len(rows) == 1 and rows[0].status == "running" and rows[0].output_json is None


def test_retry_keeps_original_call_identity_and_cannot_authorize_a_fresh_quote(admitted):
    from app.credits.errors import CreditsUnavailable
    from app.onboarding.state import record_failed, record_running
    source = admitted
    original = source.module.generation_call_id(source.intent, workspace_id="ws_firstlook",
        venture_id="ven_firstlook", slot="first")
    record_failed(source.step, "interrupted")
    retry = record_running(source.ctx.run.id, "commit.lock_charter", "commit", source.key)
    assert retry.id != source.step.id and retry.attempt == 2
    assert source.module.capture_onboarding_intent(source.ctx) == source.intent
    assert source.module.generation_call_id(source.intent, workspace_id="ws_firstlook",
        venture_id="ven_firstlook", slot="first", for_dispatch=False) == original
    with pytest.raises(CreditsUnavailable):
        _validate(source)


@pytest.mark.parametrize("intent", [None, "", "body:" + "a" * 64,
    "onboarding:obr_absent:" + "a" * 64, "onboarding:obr_absent:bad-key"])
def test_missing_or_fabricated_intent_never_grants_dispatch(admitted, intent):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable):
        admitted.module.validate_generation_intent(intent, workspace_id="ws_firstlook", venture_id="ven_firstlook")


@pytest.mark.parametrize("model,field,value", [
    ("run", "status", "cancelled"), ("run", "phase", "validate"),
    ("run", "mode", "auto"), ("run", "workspace_id", "ws_foreign"),
    ("step", "status", "failed"), ("step", "phase", "first_look"),
    ("step", "idempotency_key", "b" * 64), ("step", "attempt", 0),
    ("workspace", "settings_json", '{"blocked":true}'), ("member", "role", "viewer"),
])
def test_changed_original_state_scope_or_writer_refuses(admitted, model, field, value):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import OnboardingRun, OnboardingStep, Workspace, WorkspaceMember
    source = admitted
    kind, key = {"run": (OnboardingRun, source.ctx.run.id), "step": (OnboardingStep, source.step.id),
        "workspace": (Workspace, "ws_firstlook"), "member": (WorkspaceMember, "wm_firstlook")}[model]
    with source.db()() as session:
        setattr(session.get(kind, key), field, value)
        session.commit()
    with pytest.raises(CreditsUnavailable):
        _validate(source)


def test_duplicate_running_record_cannot_select_an_attempt_as_new_authority(admitted):
    from app.credits.errors import CreditsUnavailable
    from app.onboarding.state import record_running
    record_running(admitted.ctx.run.id, "commit.lock_charter", "commit", admitted.key)
    with pytest.raises(CreditsUnavailable):
        _validate(admitted, for_dispatch=False)


def test_changed_prior_inputs_refuse_the_saved_key(admitted):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import OnboardingStep
    with admitted.db()() as session:
        session.add(OnboardingStep(id="ost_prior", run_id=admitted.ctx.run.id,
            step_id="orient.read_brief", phase="orient", attempt=1, status="done",
            idempotency_key="prior-key", output_json='{"normalized":"changed founder input"}',
            started_at=admitted.now, finished_at=admitted.now))
        session.commit()
    with pytest.raises(CreditsUnavailable):
        _validate(admitted)


@pytest.mark.parametrize("actor", ["system", "anonymous", "local", "person_foreign"])
def test_current_actor_cannot_be_invented_from_the_run(admitted, actor):
    from app.credits.errors import CreditsUnavailable
    with admitted.bind(workspace_id="ws_firstlook", venture_id="ven_firstlook", actor=actor):
        with pytest.raises(CreditsUnavailable):
            _validate(admitted)


@pytest.mark.parametrize("slot", ["repair", "first:second", "", None])
def test_only_original_two_call_slots_are_available(admitted, slot):
    from app.credits.errors import CreditsUnavailable
    with pytest.raises(CreditsUnavailable):
        admitted.module.generation_call_id(admitted.intent, workspace_id="ws_firstlook",
            venture_id="ven_firstlook", slot=slot)


def test_cancelled_context_cannot_capture_paid_intent(admitted):
    from app.credits.errors import CreditsUnavailable
    admitted.ctx.cancel.set()
    with pytest.raises(CreditsUnavailable):
        admitted.module.capture_onboarding_intent(admitted.ctx)


def _copy():
    return {"headline": "We're building a bakery order board", "subhead": "A concept for local bakers",
        "points": [{"title": "Orders", "body": "A proposed board"}] * 3, "how_it_works": None,
        "cta": {"label": "Join the waitlist", "kind": "waitlist"}, "audience": "Local bakers",
        "name_candidates": ["Bakery Board", "Bread Desk", "Oven Notes"],
        "assumptions": ["The founder wants a concept landing page"], "fallback": False}


def _checkpoint(source, output, slot="first"):
    source.module.checkpoint_generation_output(source.intent, workspace_id="ws_firstlook",
        venture_id="ven_firstlook", slot=slot, output=output)


def _read(source, slot="first"):
    return source.module.read_generation_output(source.intent, workspace_id="ws_firstlook",
        venture_id="ven_firstlook", slot=slot)


def test_private_copy_checkpoint_is_detached_idempotent_and_preserves_other_step_output(admitted):
    from app.db.models import OnboardingStep
    source = admitted
    with source.db()() as session:
        session.get(OnboardingStep, source.step.id).output_json = '{"existing":"kept"}'
        session.commit()
    assert _read(source) == (False, None)
    output = _copy()
    _checkpoint(source, output)
    _checkpoint(source, output)
    output["headline"] = "Changed caller memory"
    exists, saved = _read(source)
    assert exists and saved == _copy()
    saved["headline"] = "Changed reader memory"
    assert _read(source) == (True, _copy())
    with source.db()() as session:
        document = json.loads(session.get(OnboardingStep, source.step.id).output_json)
        assert document == {"existing": "kept", "_firstlook_copy": {"first": _copy()}}


def test_known_bad_marker_is_distinct_from_no_checkpoint_and_cannot_be_replaced(admitted):
    from app.credits.errors import CreditsUnavailable
    _checkpoint(admitted, None)
    assert _read(admitted) == (True, None)
    assert _read(admitted, "lint-regenerate") == (False, None)
    with pytest.raises(CreditsUnavailable):
        _checkpoint(admitted, _copy())
    assert _read(admitted) == (True, None)


def test_retry_reads_only_original_attempt_copy_and_never_its_new_row(admitted):
    from app.db.models import OnboardingStep
    from app.onboarding.state import record_failed, record_running
    _checkpoint(admitted, _copy())
    record_failed(admitted.step, "interrupted")
    retry = record_running(admitted.ctx.run.id, "commit.lock_charter", "commit", admitted.key)
    assert _read(admitted) == (True, _copy())
    _checkpoint(admitted, None, "lint-regenerate")
    with admitted.db()() as session:
        assert session.get(OnboardingStep, retry.id).output_json is None
        original = json.loads(session.get(OnboardingStep, admitted.step.id).output_json)
        assert original["_firstlook_copy"] == {"first": _copy(), "lint-regenerate": None}


@pytest.mark.parametrize("changed", [
    {"usage": {"input_tokens": 1}}, {"model": "raw-sdk-model"},
    {"headline": "sk-synthetic-secret-value-that-must-not-persist"},
    {"headline": "x" * 91}, {"audience": {"raw": "SDK field"}},
    {"points": [{"title": "Orders", "body": "Concept", "raw_sdk": "private"}] * 3},
    {"assumptions": ["x" * 2049]}, {"how_it_works": [{"step": True, "text": "Concept"}]},
])
def test_unsafe_or_unbounded_copy_never_enters_original_step(admitted, changed):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import OnboardingStep
    output = copy.deepcopy(_copy()) | changed
    with pytest.raises(CreditsUnavailable):
        _checkpoint(admitted, output)
    with admitted.db()() as session:
        assert session.get(OnboardingStep, admitted.step.id).output_json is None


def test_revoked_writer_can_form_financial_lookup_but_cannot_read_or_checkpoint_copy(admitted):
    from app.credits.errors import CreditsUnavailable
    from app.db.models import WorkspaceMember
    expected = admitted.module.generation_identity(admitted.intent, slot="first")
    with admitted.db()() as session:
        session.get(WorkspaceMember, "wm_firstlook").role = "viewer"
        session.commit()
    assert admitted.module.generation_identity(admitted.intent, slot="first") == expected
    with pytest.raises(CreditsUnavailable):
        _read(admitted)
    with pytest.raises(CreditsUnavailable):
        _checkpoint(admitted, _copy())


def test_missing_current_tenancy_never_grants_paid_or_output_authority(admitted):
    from app.credits.errors import CreditsUnavailable
    from app.tenancy import reset_scope, set_scope
    token = set_scope(None)
    try:
        assert admitted.module.generation_identity(admitted.intent, slot="first")
        with pytest.raises(CreditsUnavailable):
            _validate(admitted)
        with pytest.raises(CreditsUnavailable):
            _read(admitted)
    finally:
        reset_scope(token)

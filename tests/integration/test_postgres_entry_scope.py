"""Signed entry uses persisted tenancy with the real hosted working-memory store."""
import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.security.test_core_signed_boundary import SECRET, headers


@pytest.fixture
def entry(journey_db, monkeypatch):
    monkeypatch.setenv('COS_INTERNAL_ASSERTION_SECRET', SECRET)
    monkeypatch.setenv('PROVISIONING_MODE', 'local')
    monkeypatch.setenv('COMPANY_OS_VAULT_SECRET', 'entry-scope-test-vault-secret-at-least-32-bytes')
    monkeypatch.setenv('ADMIN_REQUIRE_TOTP', '1')
    monkeypatch.setenv('ADMIN_BOOTSTRAP_EMAILS', 'entry-operator@example.test')
    from app import state
    from app.main import create_app
    from app.memory.pg_working import PostgresWorkingMemory
    from app.tenancy import current_or_none

    monkeypatch.setattr(state, 'MEM', PostgresWorkingMemory())

    async def no_naming_provider(*_args, **_kwargs):
        return None

    monkeypatch.setattr('app.intake.naming.suggest_name', no_naming_provider)
    monkeypatch.setattr('app.onboarding.spawn.start_onboarding',
                        lambda *_a, **_k: pytest.fail('No onboarding execution'))
    client = TestClient(create_app(), raise_server_exceptions=False)
    d = journey_db

    def request(path, body=None, *, actor=None, method='POST', params=None):
        before = current_or_none()
        response = client.request(method, path, json=body, params=params,
                                  headers=headers(path, method, subject=actor or d['actor']))
        assert current_or_none() == before
        return response

    return d, request


def counts(d):
    from app.db.models import ChatMessage, CreditAccount, CreditLedgerEntry, Venture, Workspace
    from app.db.supymem_models import EntryChatProposal
    with d['factory']() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in
                     (Venture, Workspace, CreditAccount, CreditLedgerEntry, ChatMessage, EntryChatProposal))


def _entry_voice_receipt(d, **scope):
    from app.analytics.events import emit
    return emit("voice.session_started", person_id=d["actor"], props={}, **scope)


def test_entry_voice_notes_are_owned_durable_once_and_never_dispatch(entry, monkeypatch):
    from app.db.models import AnalyticsEvent, IdempotencyRecord
    from sqlalchemy import select
    d, request = entry
    receipt = _entry_voice_receipt(d)
    baseline = counts(d)
    async def forbidden(*args, **kwargs):
        pytest.fail("Saving or reading voice notes must not interpret, replay or create a venture")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    full = "A scheduling tool for independent architects, with no automatic publication."
    partial = "Do not contact anyone until"
    body = {"session_event_id":receipt, "turns":[
        {"id":"full", "role":"founder", "text":full, "posted":False},
        {"id":"reply", "role":"assistant", "text":"Would you start with independent architects?"},
        {"id":"unfinished", "role":"founder", "text":partial, "final":False}]}
    first = request("/entry/voice/summary", body)
    assert first.status_code == 200, first.text
    saved = first.json()
    assert saved["read_only"] is False
    assert full in saved["body_md"] and "Unconfirmed speech, edit before sending: " + partial in saved["body_md"]
    assert "I: Would you start with independent architects?" in saved["body_md"]
    assert saved["needs_edit"] is True and saved["pending"] is False
    assert request("/entry/voice/summary", body).json() == saved
    assert request("/entry/voice/summary", body, actor="usr_foreign_entry_voice").status_code == 404
    changed = {**body, "turns":[{**body["turns"][0], "text":"A different idea"}]}
    assert request("/entry/voice/summary", changed).status_code == 409
    history = request("/entry/chat/history", method="GET").json()
    assert history["voice_notes"] == [{**saved, "partition":"fresh"}]
    assert request("/entry/chat/history", actor="usr_foreign_entry_voice", method="GET").json()["voice_notes"] == []
    with d["factory"]() as session:
        rows = list(session.scalars(select(IdempotencyRecord).where(IdempotencyRecord.key == saved["id"])))
        assert len(rows) == 1 and full in rows[0].response_json
        assert full not in session.get(AnalyticsEvent, receipt).props_json
    assert counts(d) == baseline


def test_entry_voice_note_history_keeps_old_and_target_notes_out_of_fresh_journey(entry):
    d, request = entry
    started_before = _entry_voice_receipt(d)
    proposal = preview(request, "A booking tool for independent tutors")
    committed = request("/entry/chat/confirm", {"proposal_id":proposal["id"]})
    assert committed.status_code == 200, committed.text
    target = committed.json()["venture_id"]
    def save(receipt, text, **extra):
        response = request("/entry/voice/summary", {"session_event_id":receipt,
            "turns":[{"id":"founder", "role":"founder", "text":text}], **extra})
        assert response.status_code == 200, response.text
        return response.json()["id"]
    earlier = save(started_before, "The first venture uses appointment bookings.")
    fresh = save(_entry_voice_receipt(d), "The new idea is an invoice reminder tool.")
    targeted = save(_entry_voice_receipt(d), "This note belongs to the existing booking venture.", target_venture_id=target)
    notes = request("/entry/chat/history", method="GET").json()["voice_notes"]
    assert {n["id"] for n in notes if n["partition"] == "fresh"} == {fresh}
    assert {n["id"] for n in notes if n["partition"] == "earlier"} == {earlier, targeted}
    assert {n["id"] for n in request("/entry/chat/history", method="GET", params={"target_venture_id":target}).json()["voice_notes"]} == {targeted}
    assert request("/entry/chat/history", method="GET", params={"target_venture_id":d["vid"]}).json()["voice_notes"] == []
    bad = {"session_event_id":_entry_voice_receipt(d), "target_venture_id":"ven_foreign",
        "turns":[{"id":"founder", "role":"founder", "text":"This must not be attached to a foreign venture."}]}
    assert request("/entry/voice/summary", bad).status_code == 404


def test_entry_voice_note_redacts_secrets_and_survives_generic_idempotency_cleanup(entry):
    from datetime import timedelta
    from app.db.models import IdempotencyRecord
    from app.surfaces.idempotency import purge_expired
    from core.clock import utcnow
    d, request = entry
    secret_marker = "sk-test-entry-private-credential-value"
    body = {"session_event_id":_entry_voice_receipt(d), "turns":[
        {"id":"secret", "role":"founder", "text":secret_marker}]}
    response = request("/entry/voice/summary", body)
    assert response.status_code == 200, response.text
    assert secret_marker not in response.json()["body_md"]
    key = response.json()["id"]
    with d["factory"]() as session:
        row = session.get(IdempotencyRecord, key)
        assert secret_marker not in row.response_json
        row.created_at = utcnow() - timedelta(days=2)
        session.add(IdempotencyRecord(key="ordinary-expired-cache", scope="ordinary-replay",
            response_json="{}", created_at=utcnow() - timedelta(days=2)))
        session.commit()
    assert purge_expired() == 1
    with d["factory"]() as session:
        assert session.get(IdempotencyRecord, key) is not None
        assert session.get(IdempotencyRecord, "ordinary-expired-cache") is None
    assert request("/entry/chat/history", method="GET").json()["voice_notes"][0]["id"] == key


def test_entry_voice_notes_report_existing_pending_preview_without_replaying_it(entry):
    from app.db.supymem_models import EntryChatProposal
    d, request = entry
    receipt = _entry_voice_receipt(d)
    text = "A booking tool for independent tutors"
    response = request("/entry/chat/preview", {"text":text, "request_id":f"voice_{receipt}_idea"})
    assert response.status_code == 200, response.text
    proposal = response.json()["proposal"]
    body = {"session_event_id":receipt, "turns":[{"id":"idea", "role":"founder", "text":text, "posted":False}]}
    saved = request("/entry/voice/summary", body)
    assert saved.status_code == 200 and saved.json()["needs_edit"] is False and saved.json()["pending"] is False
    baseline = counts(d)
    with d["factory"]() as session:
        session.get(EntryChatProposal, proposal["id"]).state = "classifying"
        session.commit()
    pending = request("/entry/voice/summary", body)
    assert pending.json()["id"] == saved.json()["id"] and pending.json()["pending"] is True
    notes = request("/entry/chat/history", method="GET").json()["voice_notes"]
    assert notes[0]["pending"] is True and notes[0]["body_md"] == saved.json()["body_md"]
    assert counts(d) == baseline


def test_saved_entry_voice_note_recovery_after_receipt_age_is_read_only(entry, monkeypatch):
    from datetime import timedelta
    from app.db.models import AnalyticsEvent, IdempotencyRecord
    from core.clock import utcnow
    d, request = entry
    receipt = _entry_voice_receipt(d)
    body = {"session_event_id":receipt, "turns":[{"id":"idea", "role":"founder", "text":"An owned invoice reminder idea."}]}
    saved = request("/entry/voice/summary", body)
    assert saved.status_code == 200, saved.text
    with d["factory"]() as session:
        before = session.get(IdempotencyRecord, saved.json()["id"]).response_json
        session.get(AnalyticsEvent, receipt).occurred_at = utcnow() - timedelta(minutes=21)
        session.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Expired saved-note recovery must not interpret or replay")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    recovered = request("/entry/voice/summary", body)
    assert recovered.status_code == 200 and recovered.json()["read_only"] is True
    assert recovered.json()["id"] == saved.json()["id"] and recovered.json()["body_md"] == saved.json()["body_md"]
    assert request("/entry/voice/summary", body, actor="usr_foreign_expired_note").status_code == 404
    changed = {**body, "turns":[{**body["turns"][0], "text":"A different idea after receipt expiry."}]}
    assert request("/entry/voice/summary", changed).status_code == 409
    assert request("/entry/chat/history", method="GET").json()["voice_notes"][0]["read_only"] is True
    with d["factory"]() as session:
        assert session.get(IdempotencyRecord, saved.json()["id"]).response_json == before
    missing = _entry_voice_receipt(d)
    with d["factory"]() as session:
        session.get(AnalyticsEvent, missing).occurred_at = utcnow() - timedelta(minutes=21)
        session.commit()
    assert request("/entry/voice/summary", {**body, "session_event_id":missing}).status_code == 404


def test_entry_voice_target_proof_history_access_and_target_window_are_scoped(entry):
    from datetime import timedelta
    from app.db.models import IdempotencyRecord, Workspace
    from core.clock import utcnow
    d, request = entry
    other_proposal = preview(request, "A booking tool for independent tutors")
    other = request("/entry/chat/confirm", {"proposal_id":other_proposal["id"]}).json()
    receipt = _entry_voice_receipt(d)
    text = "Use a customer interview before publication."
    previewed = request("/entry/chat/preview", {"text":text, "target_venture_id":other["venture_id"], "request_id":f"voice_{receipt}_idea"})
    assert previewed.status_code == 200, previewed.text
    body = {"session_event_id":receipt, "target_venture_id":d["vid"], "turns":[{"id":"idea", "role":"founder", "text":text, "posted":True}]}
    saved = request("/entry/voice/summary", body)
    assert saved.status_code == 200 and saved.json()["needs_edit"] is True
    key = saved.json()["id"]
    with d["factory"]() as session:
        record = session.get(IdempotencyRecord, key)
        stored = json.loads(record.response_json)
        for number in range(101):
            note = {**stored["note"], "id":f"crowding_{number}", "target_venture_id":None}
            state = {**stored, "note":note}
            session.add(IdempotencyRecord(key=note["id"], scope=record.scope.rsplit(":", 1)[0] + ":" + __import__("hashlib").sha256(b"").hexdigest(),
                response_json=json.dumps(state), created_at=utcnow() + timedelta(seconds=number + 1)))
        session.commit()
    targeted = request("/entry/chat/history", method="GET", params={"target_venture_id":d["vid"]})
    assert targeted.status_code == 200 and {item["id"] for item in targeted.json()["voice_notes"]} == {key}
    assert request("/entry/voice/summary", {**body, "target_venture_id":other["venture_id"]}).status_code == 409
    with d["factory"]() as session:
        for record in session.scalars(select(IdempotencyRecord).where(IdempotencyRecord.key.startswith("crowding_"))):
            record.created_at = utcnow() - timedelta(days=1)
        session.commit()
    assert key in {item["id"] for item in request("/entry/chat/history", method="GET").json()["voice_notes"]}
    with d["factory"]() as session:
        workspace = session.get(Workspace, d["wid"])
        workspace.archived_at = utcnow()
        session.commit()
    assert request("/entry/voice/summary", body).status_code == 404
    assert request("/entry/chat/history", method="GET", params={"target_venture_id":d["vid"]}).status_code == 404
    assert key not in {item["id"] for item in request("/entry/chat/history", method="GET").json()["voice_notes"]}


def test_saved_entry_voice_note_outlives_analytics_receipt_without_action_authority(entry, monkeypatch):
    from app.db.models import AnalyticsEvent, IdempotencyRecord
    d, request = entry
    receipt = _entry_voice_receipt(d)
    body = {"session_event_id":receipt, "turns":[{"id":"idea", "role":"founder", "text":"An invoice reminder for independent tutors."}]}
    saved = request("/entry/voice/summary", body)
    assert saved.status_code == 200, saved.text
    with d["factory"]() as session:
        before = session.get(IdempotencyRecord, saved.json()["id"]).response_json
        session.delete(session.get(AnalyticsEvent, receipt))
        session.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Saved notes without a live receipt cannot preview or dispatch")
    monkeypatch.setattr("app.intake.application.interpret", forbidden)
    recovered = request("/entry/voice/summary", body)
    assert recovered.status_code == 200 and recovered.json()["read_only"] is True
    assert recovered.json()["id"] == saved.json()["id"]
    assert recovered.json()["body_md"] == saved.json()["body_md"]
    assert request("/entry/voice/summary", {**body, "turns":[{**body["turns"][0], "posted":True}]}).status_code == 409
    assert request("/entry/voice/summary", body, actor="usr_foreign_deleted_receipt").status_code == 404
    assert request("/entry/voice/summary", {**body, "session_event_id":"ane_never_started"}).status_code == 404
    assert request("/entry/chat/history", method="GET").json()["voice_notes"][0]["read_only"] is True
    with d["factory"]() as session:
        assert session.get(IdempotencyRecord, saved.json()["id"]).response_json == before


def test_entry_voice_untargeted_proof_requires_fresh_creation_receipt(entry):
    from app.db.supymem_models import EntryChatProposal
    d, request = entry
    receipt = _entry_voice_receipt(d)
    text = "A booking tool for independent tutors"
    result = request("/entry/chat/preview", {"text":text, "request_id":f"voice_{receipt}_idea"})
    assert result.status_code == 200, result.text
    proposal_id = result.json()["proposal"]["id"]
    body = {"session_event_id":receipt, "turns":[{"id":"idea", "role":"founder", "text":text}]}
    saved = request("/entry/voice/summary", body)
    assert saved.status_code == 200 and saved.json()["needs_edit"] is False
    confirmed = request("/entry/chat/confirm", {"proposal_id":proposal_id})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created_new"] is True
    with d["factory"]() as session:
        row = session.get(EntryChatProposal, proposal_id)
        stored = json.loads(row.receipt_json)
        assert stored["created_new"] is True
    assert request("/entry/voice/summary", body).json()["needs_edit"] is False
    with d["factory"]() as session:
        row = session.get(EntryChatProposal, proposal_id)
        row.receipt_json = json.dumps({key:value for key, value in stored.items() if key != "created_new"})
        session.commit()
    # Legacy committed rows cannot prove whether the call targeted an existing venture.
    assert request("/entry/voice/summary", body).json()["needs_edit"] is True

    other = _entry_voice_receipt(d)
    words = "Use a customer interview before publication."
    result = request("/entry/chat/preview", {"text":words, "target_venture_id":d["vid"], "request_id":f"voice_{other}_idea"})
    assert result.status_code == 200, result.text
    untargeted = {"session_event_id":other, "turns":[{"id":"idea", "role":"founder", "text":words, "posted":True}]}
    assert request("/entry/voice/summary", untargeted).json()["needs_edit"] is True


def test_entry_voice_target_notes_are_hidden_after_membership_revocation(entry):
    from app.analytics.events import emit
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from core.clock import utcnow
    d, request = entry
    member = "usr_note_member_" + uuid.uuid4().hex[:12]
    ensure_subject_person(member)
    membership_id = "wsm_note_" + uuid.uuid4().hex
    with d["factory"]() as session:
        session.add(WorkspaceMember(id=membership_id, workspace_id=d["wid"], person_id=member,
            role="contributor", created_at=utcnow()))
        session.commit()
    receipt = emit("voice.session_started", person_id=member, props={})
    body = {"session_event_id":receipt, "target_venture_id":d["vid"],
        "turns":[{"id":"idea", "role":"founder", "text":"Keep the interview plan private."}]}
    saved = request("/entry/voice/summary", body, actor=member)
    assert saved.status_code == 200, saved.text
    key = saved.json()["id"]
    assert key in {item["id"] for item in request("/entry/chat/history", method="GET", actor=member).json()["voice_notes"]}
    with d["factory"]() as session:
        session.delete(session.get(WorkspaceMember, membership_id))
        session.commit()
    assert request("/entry/voice/summary", body, actor=member).status_code == 404
    assert request("/entry/chat/history", method="GET", actor=member, params={"target_venture_id":d["vid"]}).status_code == 404
    assert request("/entry/chat/history", method="GET", actor=member).json()["voice_notes"] == []


@pytest.mark.parametrize("bad_scope", ["venture", "expired", "conversation"])
def test_entry_voice_note_requires_owned_entry_receipt_and_scope(entry, bad_scope):
    from datetime import timedelta
    from app.db.models import AnalyticsEvent
    from core.clock import utcnow
    d, request = entry
    receipt = _entry_voice_receipt(d, **({"venture_id":d["vid"], "workspace_id":d["wid"]} if bad_scope == "venture" else {}))
    if bad_scope == "expired":
        with d["factory"]() as session:
            session.get(AnalyticsEvent, receipt).occurred_at = utcnow() - timedelta(minutes=21)
            session.commit()
    body = {"session_event_id":receipt, "turns":[{"id":"founder", "role":"founder", "text":"An owned entry note."}]}
    if bad_scope == "conversation":
        body["conversation_id"] = "cnv_other"
    assert request("/entry/voice/summary", body).status_code == 404


def preview(request, text):
    response = request('/entry/chat/preview', {'text': text, 'request_id': str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()['proposal']


@pytest.mark.parametrize('text,route', [
    ('Create a startup venture for bookshops with a private saved conversation.', 'startup'),
    ('Build a personal project for myself to track garden watering, not a startup.', 'personal_build'),
    ('I have an app that helps rehearsal studios manage bookings and I want GTM.', 'gtm'),
])
def test_new_chat_routes_bind_hosted_state_and_same_receipt_never_duplicates(entry, text, route):
    from app import state
    from app.db.models import ChatMessage, Venture
    from app.tenancy import bind
    d, request = entry
    baseline = counts(d)
    proposal = preview(request, text)
    assert proposal['route'] == route
    path = '/entry/chat/confirm'
    response = request(path, {'proposal_id': proposal['id']})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['status'] == 'committed' and receipt['route'] == route
    after = counts(d)
    assert after[0] == baseline[0] + 1 and after[1] == baseline[1] + 1
    assert after[4] == baseline[4] + 2
    assert request(path, {'proposal_id': proposal['id']}).json() == receipt
    assert counts(d) == after
    saved = request(f"/entry/chat/proposals/{proposal['id']}", method='GET').json()['proposal']
    assert saved['state'] == 'committed' and saved['receipt'] == receipt
    with d['factory']() as session:
        venture = session.get(Venture, receipt['venture_id'])
        assert venture.workspace_id == receipt['workspace_id']
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture.id)).all()
        assert len(messages) == 2 and all(m.workspace_id == venture.workspace_id for m in messages)
        assert next(m for m in messages if m.role == 'user').author_person_id == d['actor']
    with bind(workspace_id=receipt['workspace_id'], venture_id=receipt['venture_id'], actor=d['actor']):
        assert state.MEM[f"org:{receipt['venture_id']}"]['agents']


@pytest.mark.parametrize('mode', ['idea', 'product', 'existing_company'])
def test_direct_venture_modes_bind_only_their_persisted_scope(entry, mode):
    from app import state
    from app.tenancy import bind
    d, request = entry
    response = request('/entry', {'text': 'A private saved scheduling project for independent studios.',
                                 'explicit_mode': mode, 'name': 'Entry scope QA', 'start': False})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['committed'] is True
    with bind(workspace_id=result['workspace']['id'], venture_id=result['venture_id'], actor=d['actor']):
        assert state.MEM[f"org:{result['venture_id']}"]['agents']


def test_postcommit_failure_recovers_the_same_linked_venture_not_a_new_creation(entry, monkeypatch):
    from app.db.supymem_models import EntryChatProposal
    from app.org import summary
    d, request = entry
    proposal = preview(request, 'Create a startup for independent bookshops with a private conversation.')
    original = summary.persist_pack_org

    def fail_after_committed_venture(*_a, **_k):
        raise RuntimeError('Source-only post-commit interruption')

    monkeypatch.setattr(summary, 'persist_pack_org', fail_after_committed_venture)
    assert request('/entry/chat/confirm', {'proposal_id': proposal['id'], 'name': 'Bookshop Compass'}).status_code == 500
    with d['factory']() as session:
        row = session.get(EntryChatProposal, proposal['id'])
        assert row.state == 'committing' and row.target_venture_id
        # Durable intake/name/lease metadata precedes the final receipt. An
        # interrupted commit must not expose it as a successful continuation.
        metadata = json.loads(row.receipt_json)
        assert metadata['intake']['path'] == 'startup'
        assert metadata['chosen_name'] == 'Bookshop Compass'
        assert metadata['created_new'] is True and metadata['commit_owner']
        assert 'status' not in metadata and 'venture_id' not in metadata
        linked = row.target_venture_id
    saved = request(f"/entry/chat/proposals/{proposal['id']}", method='GET').json()['proposal']
    assert saved['state'] == 'committing' and saved['receipt'] is None
    failed = counts(d)
    monkeypatch.setattr(summary, 'persist_pack_org', original)
    response = request('/entry/chat/confirm', {'proposal_id': proposal['id']})
    assert response.status_code == 200, response.text
    assert response.json()['venture_id'] == linked
    assert response.json()['name'] == 'Bookshop Compass'
    recovered = counts(d)
    assert recovered[:4] == failed[:4] and recovered[4] == failed[4] + 2
    assert request('/entry/chat/confirm', {'proposal_id': proposal['id']}).json() == response.json()
    assert counts(d) == recovered


def test_existing_target_and_foreign_proposal_cannot_create_or_cross_scope(entry):
    d, request = entry
    proposal = preview(request, 'I already have a product for studios and I want to get customers through GTM.')
    foreign = counts(d)
    assert request('/entry/chat/confirm', {'proposal_id': proposal['id']}, actor='usr_foreign_entry').status_code == 404
    assert counts(d) == foreign
    existing = request('/entry/chat/preview', {'text': 'I want GTM for this existing product.',
                                             'target_venture_id': d['vid']}).json()['proposal']
    before = counts(d)
    confirmed = request('/entry/chat/confirm', {'proposal_id': existing['id']})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['venture_id'] == d['vid'] and counts(d)[:2] == before[:2]


def test_tampered_actor_and_persisted_workspace_are_refused_and_scope_restored(entry):
    from app.entry import service
    from app.entry.route import route
    from app.tenancy import bind, current_or_none
    d, _request = entry
    before = counts(d)
    with bind(workspace_id=d['wid'], venture_id=d['vid'], actor=d['actor']):
        incoming = current_or_none()
        with pytest.raises(HTTPException) as mismatch:
            asyncio.run(service.commit(route('Private bookshop scheduling startup', explicit_mode='idea'),
                                       text='Private bookshop scheduling startup', actor_id='usr_tampered_entry',
                                       mode_options={'start': False}))
        assert mismatch.value.status_code == 403 and counts(d) == before
        with pytest.raises(HTTPException) as foreign:
            with service.venture_scope(d['vid'], actor_id=d['actor'], workspace_id='wsp_foreign'):
                pytest.fail('Foreign workspace binding')
        assert foreign.value.status_code == 404 and current_or_none() == incoming
        with pytest.raises(RuntimeError, match='owned test interruption'):
            with service.venture_scope(d['vid'], actor_id=d['actor'], workspace_id=d['wid']):
                raise RuntimeError('owned test interruption')
        assert current_or_none() == incoming


@pytest.mark.parametrize('tamper', ['archived', 'receipt_workspace', 'linked_target', 'cleared_target'])
def test_saved_committed_outcome_rechecks_current_owned_venture_before_reuse(entry, tamper):
    from app.db.models import Venture
    from app.db.supymem_models import EntryChatProposal
    from core.clock import utcnow
    d, request = entry
    proposal = preview(request, 'Create a startup for private bookshop inventory planning.')
    response = request('/entry/chat/confirm', {'proposal_id': proposal['id']})
    assert response.status_code == 200, response.text
    with d['factory']() as session:
        row = session.get(EntryChatProposal, proposal['id'])
        if tamper == 'archived':
            session.get(Venture, response.json()['venture_id']).archived_at = utcnow()
        elif tamper == 'receipt_workspace':
            receipt = json.loads(row.receipt_json)
            receipt['workspace_id'] = 'wsp_foreign_entry'
            row.receipt_json = json.dumps(receipt)
        elif tamper == 'linked_target':
            row.target_venture_id = d['vid']
        else:
            row.target_venture_id = None
        session.commit()
    before = counts(d)
    assert request(f"/entry/chat/proposals/{proposal['id']}", method='GET').status_code == 404
    assert request('/entry/chat/confirm', {'proposal_id': proposal['id']}).status_code == 404
    assert counts(d) == before

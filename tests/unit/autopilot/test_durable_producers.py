"""Actual source mutations with closed provider seams; no native/model/network effects."""
import asyncio
import json

import pytest

from tests.unit.autopilot import test_god_mode as harness


@pytest.fixture(name='env')
def producer_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "1")
    for client, vid in harness.env.__wrapped__(tmp_path, monkeypatch):
        from app.autopilot import service
        from app.build.device_desktop_pilot import ALLOWLIST_ENV
        from app.db.supymem_models import DesktopDevice
        from app.state import db

        actor = service._owner(vid)
        monkeypatch.setenv(ALLOWLIST_ENV, json.dumps([[actor, "owned_device"]]))
        with db()() as session:
            session.add(DesktopDevice(id="owned_device", person_id=actor,
                name="Owned synthetic Mac", platform="macos", app_version="0.1.84", revoked_at=None))
            session.commit()
        yield client, vid


@pytest.mark.parametrize('email,n', [(False,1),(True,1),(True,30)])
def test_actual_owned_outreach_batch_has_draft_evidence_without_send(env, monkeypatch, email, n):
    client, vid = env
    from app.autopilot import service, outreach
    from app.db.models import GtmContact, Venture, VentureDocument
    from app.state import db
    from core.clock import utcnow
    harness._on(client, vid)
    with db()() as s:
        wid = s.get(Venture, vid).workspace_id
        for i in range(n):
            s.add(GtmContact(id=f'owned_contact_{i}', workspace_id=wid, email=f'owner{i}@synthetic.example' if email else None,
            email_confidence='risky', lawful_basis='legitimate_interest', source=f'venture:{vid}:research',
            enrichment_json=json.dumps({'business':'Owned synthetic business','instagram':'synthetic_handle'}),
            stage_changed_at=utcnow(),created_at=utcnow()))
        s.commit()
    async def no_model(*args):
        return {}
    monkeypatch.setattr(outreach, '_llm_drafts', no_model)
    monkeypatch.setattr(outreach, '_send_one', lambda *args: pytest.fail('Drafting must never send'))
    done=[]
    _, stop = asyncio.run(service._execute(vid,'outreach.draft',{'batch':True,'n':n},'Owned draft',service._owner(vid),done))
    with db()() as s:
        docs=s.query(VentureDocument).filter_by(venture_id=vid,kind=outreach.KIND).all()
        assert len(docs)==n and all(d.body_md for d in docs)
        assert json.loads(docs[0].provenance_json)['state']==('draft' if email else 'manual')
    assert stop is None and done==['outreach.draft']
    assert not service._unresolved_admission(vid,'outreach.draft')


def test_actual_mac_approval_has_only_owned_approval_evidence(env):
    client, vid = env
    from app.admin.authn import CallerContext
    from app.autopilot import service
    from app.build import device_orders
    from app.credits.accounts import ensure_account
    from app.db.models import VentureDocument
    from app.state import db
    actor=service._owner(vid)
    harness._on(client,vid,allow_mac=True)
    ensure_account(vid)
    caller=CallerContext(actor,'','user','/test','POST',9999999999,'owned',device_id='owned_device')
    order=device_orders.prepare_desktop_from_chat(vid,caller,'Use Safari to read https://example.org and report the title.',
        'com.apple.Safari',123,'123456789',44,'Owned synthetic surface')
    done=[]
    _,stop=asyncio.run(service._execute(vid,'mac.approve',{'order_id':order['id'],'review_digest':order['review_digest']},
        'Owned approval',actor,done))
    with db()() as s:
        body=json.loads(s.get(VentureDocument,order['id']).provenance_json)
        assert body['state']=='approved' and not body.get('claimed_at') and not body.get('execution_id')
        assert body['actor_id']==actor and body['device_id']=='owned_device'
    assert stop is None and done==['mac.approve']
    assert not service._unresolved_admission(vid,'mac.approve')


@pytest.mark.parametrize('tamper', ['metadata','content','delete'])
def test_document_receipt_rechecks_metadata_content_and_existence(env,tamper):
    _,vid=env
    from app.autopilot import outreach,receipts,service
    from app.autopilot.authority import founder_scope
    from app.db.models import VentureDocument
    from app.state import db
    with founder_scope(vid,service._owner(vid)):
        with receipts.collect(vid) as evidence:
            did=outreach._save_draft(vid,{'to':'synthetic','body':'Owned draft','state':'draft'})
        with db()() as s:
            row=s.get(VentureDocument,did)
            if tamper=='metadata':
                row.provenance_json=json.dumps({'state':'sent'})
            elif tamper=='content':
                row.body_md='Different'
            else:
                s.delete(row)
            s.commit()
        assert evidence.outcome()=='held'


def test_outreach_cannot_update_a_foreign_document(env):
    client,vid=env
    from app.autopilot import outreach
    from app.db.models import VentureDocument
    from app.state import db
    other=client.post('/ventures',json={'raw_idea':'Other owned synthetic venture','start':False}).json()['id']
    did=outreach._save_draft(other,{'to':'synthetic','body':'Original','state':'draft'})
    with pytest.raises(ValueError,match='outreach_draft_scope_mismatch'):
        outreach._save_draft(vid,{'to':'synthetic','body':'Changed','state':'draft'},did)
    with db()() as s:
        assert s.get(VentureDocument,did).body_md=='Original'


def test_document_proof_remains_bounded_after_maximum_supported_batch(env):
    _,vid=env
    from app.autopilot import outreach,receipts,service
    from app.autopilot.authority import founder_scope
    with founder_scope(vid,service._owner(vid)):
        with receipts.collect(vid) as evidence:
            for i in range(33):
                outreach._save_draft(vid,{'to':f'synthetic{i}','body':'Owned','state':'draft'})
        assert evidence.outcome()=='held'


def _prepared_order(env):
    _,vid=env
    from app.admin.authn import CallerContext
    from app.autopilot import service
    from app.build import device_orders
    from app.credits.accounts import ensure_account
    actor=service._owner(vid)
    ensure_account(vid)
    caller=CallerContext(actor,'','user','/test','POST',9999999999,'owned',device_id='owned_device')
    order=device_orders.prepare_desktop_from_chat(vid,caller,'Use Safari to read https://example.org and report the title.',
        'com.apple.Safari',123,'123456789',44,'Owned synthetic surface')
    return vid,actor,caller,order


@pytest.mark.parametrize('wrong',['actor','workspace','venture'])
def test_mac_approval_cannot_impersonate_order_owner_from_context(env,wrong):
    from app.autopilot import lifecycle
    from app.db.models import VentureDocument
    from app.state import db
    from app.tenancy import bind
    vid,actor,_,order=_prepared_order(env)
    with db()() as s:
        wid=json.loads(s.get(VentureDocument,order['id']).provenance_json)['workspace_id']
    with bind(workspace_id='other' if wrong=='workspace' else wid,
              venture_id='other' if wrong=='venture' else vid,actor='other' if wrong=='actor' else actor):
        with pytest.raises(PermissionError,match='autopilot_scope_mismatch'):
            lifecycle.approve_mac_order(vid,{'order_id':order['id']})
    with db()() as s:
        assert json.loads(s.get(VentureDocument,order['id']).provenance_json)['state']=='pending'


def test_mac_claim_race_cannot_be_certified_as_approval_only(env,monkeypatch):
    client,_=env
    from app.autopilot import service
    from app.build import device_orders
    vid,actor,caller,order=_prepared_order(env)
    harness._on(client,vid,allow_mac=True)
    original=device_orders.transition
    def claim_between_transition_and_readback(*args):
        approved=original(*args)
        if args[-1]=='approve':
            original(vid,caller,order['id'],approved['review_digest'],'claim')
        return approved
    monkeypatch.setattr(device_orders,'transition',claim_between_transition_and_readback)
    done=[]
    _,stop=asyncio.run(service._execute(vid,'mac.approve',{'order_id':order['id'],
        'review_digest':order['review_digest']},'Owned approval',actor,done))
    assert stop=='held' and done==[] and service._unresolved_admission(vid,'mac.approve')


@pytest.mark.parametrize('reviewed_digest', [None, '', 'not-the-reviewed-digest'])
def test_mac_approval_requires_the_exact_digest_the_founder_reviewed(env,reviewed_digest):
    from app.autopilot import lifecycle,service
    from app.autopilot.authority import founder_scope
    from app.db.models import VentureDocument
    from app.state import db
    vid,actor,_,order=_prepared_order(env)
    with founder_scope(vid,actor):
        reply=lifecycle.approve_mac_order(vid,{'order_id':order['id'],'review_digest':reviewed_digest})
    assert reply['kind']=='needs_you'
    with db()() as session:
        saved=json.loads(session.get(VentureDocument,order['id']).provenance_json)
        assert saved['state']=='pending'
        assert not saved.get('approved_at') and not saved.get('execution_id')
    assert not service._unresolved_admission(vid,'mac.approve')


def test_stale_mac_review_reserves_nothing_and_fresh_review_can_approve(env):
    from app.autopilot import service,guard
    from app.db.models import VentureDocument
    from app.state import db
    client,_=env
    vid,actor,_,order=_prepared_order(env)
    harness._on(client,vid,allow_mac=True)
    done=[]
    _,stop=asyncio.run(service._execute(vid,'mac.approve',{'order_id':order['id'],
        'review_digest':'stale'},'Stale approval',actor,done,True))
    assert stop=='mac_review_required' and done==[]
    assert not any(row['outcome']=='attempt' for row in service.timeline(vid) if row['action']=='mac.approve')
    assert guard.get_settings(vid)['spent_today_micro']==0
    assert not service._unresolved_admission(vid,'mac.approve')
    with db()() as session:
        assert json.loads(session.get(VentureDocument,order['id']).provenance_json)['state']=='pending'
    _,stop=asyncio.run(service._execute(vid,'mac.approve',{'order_id':order['id'],
        'review_digest':order['review_digest']},'Fresh approval',actor,done,True))
    assert stop is None and done==['mac.approve']


def _prepared_lease_order(env, marker):
    from app.build import device_orders
    from app.db.models import VentureDocument
    from app.state import db
    vid, actor, _, order = _prepared_order(env)
    with db()() as session:
        row = session.get(VentureDocument, order["id"])
        saved = json.loads(row.provenance_json)
        saved["lease_contract"] = marker
        saved["review_digest"] = device_orders._digest(saved)
        row.provenance_json = json.dumps(saved)
        session.commit()
    return vid, actor, saved


@pytest.mark.parametrize("marker", [None, {"protocol": "unsupported"},
    {"schema_version": 1, "protocol": "task_step_lease_v1", "lease_seconds": 60, "heartbeat_seconds": 15}])
@pytest.mark.parametrize("human", [False, True])
def test_actual_new_lease_mac_approval_needs_native_review_before_any_admission(env, monkeypatch, marker, human):
    from sqlalchemy import select
    from app.autopilot import guard, lifecycle, receipts, service
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.db.models import CreditAccount, CreditReservation, VentureDocument
    from app.state import db
    client, _ = env
    vid, actor, saved = _prepared_lease_order(env, marker)
    harness._on(client, vid, allow_mac=True)
    monkeypatch.setattr(device_orders, "transition", lambda *args, **kwargs: pytest.fail("Native approval was admitted"))
    monkeypatch.setattr(receipts, "saved_document", lambda *args, **kwargs: pytest.fail("Approval receipt was admitted"))
    with db()() as session:
        counters = session.execute(select(CreditAccount.id, CreditAccount.reserved_micro, CreditAccount.spent_micro)).all()
    payload = {"order_id": saved["id"], "review_digest": saved["review_digest"],
        "lease_contract_digest": "a" * 64, "task_step_lease_v1": True, "native_negotiated": True,
        "approved_by_founder": True, "lease_contract": None}
    done, used = [], [0]
    token = service._tick_used.set(used)
    try:
        _, stop = asyncio.run(service._execute(vid, "mac.approve", payload, "Owned approval", actor, done, human))
    finally:
        service._tick_used.reset(token)
    assert stop == "mac_native_approval_required" and done == [] and used == [0]
    events = [row for row in service.timeline(vid) if row["action"] == "mac.approve"]
    assert len(events) == 1 and events[0]["outcome"] == "blocked" and events[0]["credits"] == 0
    assert events[0]["why"] == "Open the task review in the updated Supymem app on your Mac."
    assert guard.get_settings(vid)["spent_today_micro"] == 0
    assert not service._unresolved_admission(vid, "mac.approve")
    with founder_scope(vid, actor), receipts.collect(vid) as evidence:
        reply = lifecycle.approve_mac_order(vid, payload)
    assert reply["kind"] == "needs_you" and reply["code"] == "mac_native_approval_required"
    assert reply["body_md"] == "Open the task review in the updated Supymem app on your Mac."
    assert not (evidence.jobs or evidence.documents or evidence.persisted or evidence.failure)
    with db()() as session:
        assert json.loads(session.get(VentureDocument, saved["id"]).provenance_json) == saved
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.request_id == saved["id"])) is None
        assert session.execute(select(CreditAccount.id, CreditAccount.reserved_micro, CreditAccount.spent_micro)).all() == counters
    assert saved["state"] == "pending" and not any(key in saved for key in
        ("approved_at", "claimed_at", "execution_id", "reservation_id"))


@pytest.mark.parametrize("unknown", [False, True])
def test_new_lease_mac_preflight_preserves_stale_review_and_prior_unknown_precedence(env, monkeypatch, unknown):
    from sqlalchemy import select
    from app.autopilot import service
    from app.build import device_orders
    from app.db.models import CreditReservation, VentureDocument
    from app.state import db
    client, _ = env
    vid, actor, saved = _prepared_lease_order(env, None)
    harness._on(client, vid, allow_mac=True)
    monkeypatch.setattr(device_orders, "transition", lambda *args, **kwargs: pytest.fail("Native approval was admitted"))
    if unknown:
        service.record(vid, "mac.approve", "Earlier admission", outcome="attempt", event_id="apv_native_unknown")
    done = []
    _, stop = asyncio.run(service._execute(vid, "mac.approve", {"order_id": saved["id"], "review_digest": "stale"},
        "Stale approval", actor, done, True))
    assert stop == ("held" if unknown else "mac_review_required") and done == []
    assert service._unresolved_admission(vid, "mac.approve") is unknown
    events = [row for row in service.timeline(vid) if row["action"] == "mac.approve"]
    assert len(events) == 1 and events[0]["outcome"] == ("attempt" if unknown else "blocked")
    with db()() as session:
        assert json.loads(session.get(VentureDocument, saved["id"]).provenance_json) == saved
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.request_id == saved["id"])) is None


@pytest.mark.parametrize("change", ["actor", "workspace", "identity", "review", "snapshot", "device", "expired", "claimed", "history", "scope"])
def test_native_review_move_requires_fresh_owned_pending_source(env, change):
    from app.autopilot import actions, lifecycle
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    from app.state import db
    from app.tenancy import bind
    vid, actor, saved = _prepared_lease_order(env, None)
    with founder_scope(vid, actor):
        move = lifecycle.native_mac_review_move(vid, saved["id"])
        assert move is not None and move.human and move.action is None
        assert move.title == "Review on Mac" and move.human_href == f"/v/{vid}/device-work-orders/{saved['id']}"
        proposal = actions._to_proposal(move)
        assert proposal.human_only and proposal.kind == "human" and proposal.href == move.human_href
    with db()() as session:
        row = session.get(VentureDocument, saved["id"])
        body = json.loads(row.provenance_json)
        if change == "device":
            session.get(DesktopDevice, body["device_id"]).revoked_at = row.created_at
        elif change != "scope":
            if change == "actor":
                body["actor_id"] = "foreign_actor"
            elif change == "workspace":
                body["workspace_id"] = "foreign_workspace"
            elif change == "identity":
                body["id"] = "foreign_order"
            elif change == "snapshot":
                body["task_snapshot"]["venture_created_at"] = "different_snapshot"
            elif change == "expired":
                body["expires_at"] = "2000-01-01T00:00:00+00:00"
            elif change == "claimed":
                body["execution_id"] = "dex_unconfirmed"
            elif change == "history":
                body["invocations"] = {}
            body["review_digest"] = "stale" if change == "review" else device_orders._digest(body)
            row.provenance_json = json.dumps(body)
        session.commit()
    context = (bind(workspace_id="foreign_workspace", venture_id=vid, actor=actor)
               if change == "scope" else founder_scope(vid, actor))
    with context:
        assert lifecycle.native_mac_review_move(vid, saved["id"]) is None
        assert not any(move.payload.get("order_id") == saved["id"] for move in lifecycle.mac_moves(vid))


def test_actual_native_review_loop_asks_once_with_one_navigation_primary_and_no_admission(env, monkeypatch):
    from sqlalchemy import select
    from app.autopilot import lifecycle, service
    from app.autopilot.authority import founder_scope
    from app.db.models import ChatMessage, CreditReservation, VentureDocument
    from app.needs_you import service as ny
    from app.state import db
    client, _ = env
    vid, actor, saved = _prepared_lease_order(env, {"protocol": "unsupported"})
    harness._on(client, vid, allow_mac=True)
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: None)
    async def no_automatic_approval(*args, **kwargs):
        pytest.fail("Human review was dispatched as mac.approve")
    monkeypatch.setattr(service, "_execute", no_automatic_approval)
    done = []
    with founder_scope(vid, actor):
        move = lifecycle.native_mac_review_move(vid, saved["id"])
        assert lifecycle._step_move({"key": move.key, "title": "Wrong title", "href": "/wrong"}, vid) == move
        asyncio.run(service._approve_safe_mac_orders(vid, actor, done))
        service.clear_stale_asks(vid, "research.start")
        asyncio.run(service._approve_safe_mac_orders(vid, actor, done))
    requests = ny.list_open(vid)
    assert len(requests) == 1 and requests[0]["reason_code"] == "mac_native_approval_required"
    assert requests[0]["blocking_ref"] == f"godmode:human:mac.{saved['id']}"
    assert ny.resolve(requests[0]["id"], venture_id=vid, actor=actor) is None
    assert ny.list_open(vid)[0]["id"] == requests[0]["id"] and done == []
    with db()() as session:
        cards = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "needs_you")).all()
        assert len(cards) == 1 and "Continue" not in cards[0].body_md
        card_actions = json.loads(cards[0].actions_json)
        assert [action for action in card_actions if action.get("style") == "primary"] == [
            {"label": "Review on Mac", "href": move.human_href, "style": "primary"}]
        assert not any(action.get("action") in {"needs_you.resolve", "mac.approve"} for action in card_actions)
        assert json.loads(session.get(VentureDocument, saved["id"]).provenance_json) == saved
        assert session.scalar(select(CreditReservation.id).where(CreditReservation.request_id == saved["id"])) is None
    assert not service._unresolved_admission(vid, "mac.approve")
    assert not any(event["action"] == "mac.approve" and event["outcome"] == "attempt" for event in service.timeline(vid))


@pytest.mark.parametrize("retired", [False, True])
def test_native_review_reminder_rechecks_source_and_retires_only_actual_saved_dependency(env, monkeypatch, retired):
    from datetime import timedelta
    from sqlalchemy import select
    from app.autopilot import lifecycle, service
    from app.autopilot.authority import founder_scope
    from app.db.models import ChatMessage, VentureDocument
    from app.needs_you import service as ny
    from app.state import db
    from core.clock import utcnow
    vid, actor, saved = _prepared_lease_order(env, None)
    notices = []
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: notices.append(kwargs))
    with founder_scope(vid, actor):
        service.needs_human(vid, lifecycle.native_mac_review_move(vid, saved["id"]))
    request = ny.list_open(vid)[0]
    with db()() as session:
        row = session.get(VentureDocument, saved["id"])
        body = json.loads(row.provenance_json)
        if retired:
            body["state"] = "cancelled"
        else:
            body["review_digest"] = "unconfirmed_review"
        row.provenance_json = json.dumps(body)
        session.commit()
    before = len(notices)
    ny.remind_due(now=utcnow() + timedelta(minutes=6))
    assert len(notices) == before
    if retired:
        assert ny.list_open(vid) == []
        with db()() as session:
            card = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "needs_you_resolved"))
            assert card is not None and card.actions_json is None
    else:
        assert ny.list_open(vid)[0]["id"] == request["id"]
        with founder_scope(vid, actor):
            service.clear_stale_asks(vid, "experiment.plan")
        assert ny.list_open(vid)[0]["id"] == request["id"]


@pytest.mark.parametrize("change", ["missing", "corrupt", "actor", "workspace", "venture", "owner", "blocked", "device",
    "stop_unknown", "inference_unknown", "calling", "uncertain", "effect_admitted", "effect_calling", "effect_uncertain",
    "task_deferred", "task_uncertain", "expired_unknown", "context"])
def test_unconfirmed_or_unknown_native_source_cannot_retire_ask_or_queue_new_work(env, monkeypatch, change):
    from contextlib import nullcontext
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.autopilot import lifecycle, service
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.db.models import Job, Person, VentureDocument, Workspace
    from app.db.supymem_models import DesktopDevice
    from app.needs_you import service as ny
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow
    vid, actor, saved = _prepared_lease_order(env, None)
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: None)
    with founder_scope(vid, actor):
        service.needs_human(vid, lifecycle.native_mac_review_move(vid, saved["id"]))
    request = ny.list_open(vid)[0]
    with db()() as session:
        row = session.get(VentureDocument, saved["id"])
        body = json.loads(row.provenance_json)
        body["state"] = "cancelled"
        if change == "missing":
            session.delete(row)
        elif change == "corrupt":
            row.provenance_json = "{"
        else:
            if change in {"actor", "workspace", "venture"}:
                body[change + "_id"] = "foreign_" + change
            elif change == "owner":
                session.add(Person(id="foreign_owner", display_name="Foreign owner", created_at=utcnow()))
                session.get(Workspace, saved["workspace_id"]).owner_id = "foreign_owner"
            elif change == "blocked":
                session.get(Workspace, saved["workspace_id"]).settings_json = json.dumps({"blocked": True})
            elif change == "device":
                session.get(DesktopDevice, saved["device_id"]).revoked_at = utcnow()
            elif change == "stop_unknown":
                body["stop_reconciliation_required"] = True
            elif change == "inference_unknown":
                body["inference_reconciliation_required"] = True
            elif change == "expired_unknown":
                body.update(state="pending", expires_at="2000-01-01T00:00:00+00:00", stop_reconciliation_required=True)
            elif change in {"calling", "uncertain"}:
                body["invocations"] = {"original_call": {"state": change}}
            elif change in {"effect_admitted", "effect_calling", "effect_uncertain"}:
                body["invocations"] = {"original_call": {"state": "completed", "effect": {"state": change.split("_")[1]}}}
            elif change in {"task_deferred", "task_uncertain"}:
                body["invocations"] = {"original_call": {"state": "completed", "task_usage_projection": change.split("_")[1]}}
            body["review_digest"] = device_orders._digest(body)
            row.provenance_json = json.dumps(body)
        session.commit()
        jobs_before = session.scalar(select(func.count()).select_from(Job))
    context = bind(workspace_id="foreign_workspace", venture_id=vid, actor=actor) if change == "context" else nullcontext()
    with context:
        assert lifecycle.native_mac_review_retired(vid, saved["id"]) is False
        assert ny._native_review_move(request) is None
        assert ny.resolve(request["id"], venture_id=vid, actor=actor) is None
        service.clear_stale_asks(vid, "experiment.plan")
        ny.remind_due(now=utcnow() + timedelta(minutes=6))
    assert ny.list_open(vid)[0]["id"] == request["id"]
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before


@pytest.mark.parametrize("reference", ["", "godmode:human:mac.", "godmode:human:mac. ", "other:request", "godmode:mac.approve", "open_key"])
def test_malformed_native_ask_never_falls_back_to_generic_continue_or_relinks_source(env, monkeypatch, reference):
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.autopilot import lifecycle, service
    from app.autopilot.authority import founder_scope
    from app.db.models import Job
    from app.db.supymem_models import NeedsYouRequest
    from app.needs_you import service as ny
    from app.state import db
    from core.clock import utcnow
    vid, actor, saved = _prepared_lease_order(env, None)
    deliveries = []
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: None)
    with founder_scope(vid, actor):
        service.needs_human(vid, lifecycle.native_mac_review_move(vid, saved["id"]))
    request = ny.list_open(vid)[0]
    with db()() as session:
        row = session.get(NeedsYouRequest, request["id"])
        if reference == "open_key":
            row.open_key = "unconfirmed_original_source"
        else:
            row.blocking_ref = reference
        session.commit()
        jobs_before = session.scalar(select(func.count()).select_from(Job))
    request = ny.list_open(vid)[0]
    monkeypatch.setattr("app.chat.milestones.post_milestone", lambda *args, **kwargs: deliveries.append("chat"))
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: deliveries.append("notice"))
    assert ny._mac_review_order(request) is not None
    assert ny._native_review_move(request) is None
    ny._deliver(request, extra=[{"label": "Continue", "action": "needs_you.resolve", "style": "primary"}])
    assert ny.resolve(request["id"], venture_id=vid, actor=actor) is None
    service.clear_stale_asks(vid, "experiment.plan")
    ny.remind_due(now=utcnow() + timedelta(minutes=6))
    assert deliveries == [] and ny.list_open(vid)[0] == request
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before


@pytest.mark.parametrize("bad_id", [None, "", 123, "missing"])
def test_saved_mac_move_bad_identity_never_selects_another_owned_review(env, monkeypatch, bad_id):
    from app.autopilot import lifecycle
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow
    vid, actor, saved = _prepared_lease_order(env, None)
    corrupt = dict(saved)
    if bad_id == "missing":
        corrupt.pop("id")
    else:
        corrupt["id"] = bad_id
    with db()() as session:
        session.add(VentureDocument(id="dwo_corrupt_identity", venture_id=vid, kind=device_orders.KIND,
            title="Unconfirmed source", provenance_json=json.dumps(corrupt), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    original = lifecycle.native_mac_review_move
    selected = []
    def tracked(venture_id, order_id=None):
        selected.append(order_id)
        return original(venture_id, order_id)
    monkeypatch.setattr(lifecycle, "native_mac_review_move", tracked)
    with founder_scope(vid, actor):
        moves = lifecycle.mac_moves(vid)
    assert selected == [saved["id"]]
    assert len(moves) == 1 and moves[0].key == f"mac.{saved['id']}"
    assert moves[0].payload["order_id"] == saved["id"]


def test_native_review_http_denial_has_no_delivery_reminder_or_wakeup_fallback(env, monkeypatch):
    from datetime import timedelta
    from sqlalchemy import func, select
    from app.autopilot import lifecycle, service
    from app.autopilot.authority import founder_scope
    from app.db.models import Job, Workspace
    from app.needs_you import service as ny
    from app.state import db
    from core.clock import utcnow
    vid, actor, saved = _prepared_lease_order(env, None)
    deliveries = []
    monkeypatch.setenv("NEEDS_YOU_QUIET_HOURS", "0-0")
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.email.triggers.on_needs_you", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: None)
    with founder_scope(vid, actor):
        service.needs_human(vid, lifecycle.native_mac_review_move(vid, saved["id"]))
    request = ny.list_open(vid)[0]
    with db()() as session:
        session.get(Workspace, saved["workspace_id"]).settings_json = json.dumps({"blocked": True})
        session.commit()
        jobs_before = session.scalar(select(func.count()).select_from(Job))
    monkeypatch.setattr("app.chat.milestones.post_milestone", lambda *args, **kwargs: deliveries.append("chat"))
    monkeypatch.setattr("app.desktop_notices.enqueue", lambda *args, **kwargs: deliveries.append("notice"))
    assert ny._native_review_move(request) is None
    assert lifecycle.native_mac_review_retired(vid, saved["id"]) is False
    ny._deliver(request)
    assert ny.resolve(request["id"], venture_id=vid, actor=actor) is None
    service.clear_stale_asks(vid, "experiment.plan")
    ny.remind_due(now=utcnow() + timedelta(minutes=6))
    asyncio.run(service._approve_safe_mac_orders(vid, actor, []))
    assert deliveries == [] and ny.list_open(vid)[0] == request
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == jobs_before

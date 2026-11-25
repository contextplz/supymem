"""The PMF loop: decision rules, the iteration state machine, parallel starts, floors, the journal."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.unit.autopilot.test_god_mode import _fake_chat, _on, env as _shared_env

from tests.unit.autopilot.test_lifecycle import _msgs

env = _shared_env  # publish shared pytest fixture without redefining its import


@pytest.mark.parametrize("baseline,target,measured,hours,expect", [
    (0, 5, 5, 1, "keep"),      # target reached early: keep immediately
    (0, 5, 9, 80, "keep"),
    (0, 5, 2, 10, None),       # still inside the window
    (0, 5, 2, 72, "pivot"),    # moved, not enough: same assumption, new variant
    (3, 8, 3, 72, "kill"),     # did not move: drop the assumption
    (3, 8, 1, 100, "kill"),
])
def test_decision_rules(env, baseline, target, measured, hours, expect):
    from app.autopilot import pmf

    assert pmf.decide(baseline, target, measured, hours) == expect


class Metrics:
    def __init__(self, monkeypatch):
        self.v = {"waitlist_signups": 0, "outbound_replies": 0, "outbound_touches": 0}
        monkeypatch.setattr("app.autopilot.pmf.read_metric", lambda _vid, m: self.v[m])


def _runner(ran, ok=True):
    async def run(name, payload):
        ran.append((name, payload))
        return ok
    return run


def _age(vid, hours):
    """Make every running iteration look `hours` old."""
    from app.autopilot import pmf
    from core.clock import utcnow

    for it in pmf.journal(vid):
        it["started_at"] = (utcnow() - timedelta(hours=hours)).isoformat()
        pmf._save(vid, it, it["doc_id"])


def test_runs_cheap_experiments_in_parallel_and_decides_from_real_data(env, monkeypatch):
    client, vid = env
    from app.autopilot import pmf

    m, ran = Metrics(monkeypatch), []
    started = asyncio.run(pmf.advance(vid, _runner(ran), allow_send=True, idea="invoice chaser"))
    assert [i["variable"] for i in started] == ["positioning"]  # one start a pass
    assert ran[0][0] == "site.rewrite" and ran[1][0] == "site.publish"
    started = asyncio.run(pmf.advance(vid, _runner(ran), allow_send=True, idea="invoice chaser"))
    assert [i["variable"] for i in started] == ["icp"] and ran[2][0] == "outreach.draft"
    # one live test per metric: sign-ups and replies are each moved by one change at a time, so a win has one owner
    assert asyncio.run(pmf.advance(vid, _runner(ran), allow_send=True, idea="x")) == []

    m.v.update(waitlist_signups=7, outbound_replies=1, outbound_touches=10)  # positioning wins; icp moved a little
    _age(vid, 80)
    out = asyncio.run(pmf.advance(vid, _runner(ran), allow_send=True, idea="x"))
    decided = {i["variable"]: i["decision"] for i in out if i["state"] != "running"}
    assert decided == {"positioning": "keep", "icp": "pivot"}
    # a pivot tries the next variant of the same variable; a kept variable is left alone
    nxt = [i for i in pmf.journal(vid) if i["state"] == "running"]
    assert {(i["variable"], i["variant"]) for i in nxt} == {("icp", 1)}
    assert "KEEP" in pmf.describe([i for i in pmf.journal(vid) if i["variable"] == "positioning"][0])


def test_a_kill_is_recorded_and_a_floor_refusal_starts_nothing(env, monkeypatch):
    client, vid = env
    from app.autopilot import pmf

    Metrics(monkeypatch)
    assert asyncio.run(pmf.advance(vid, _runner([], ok=False), allow_send=False, idea="x")) == []  # a floor said no
    assert pmf.journal(vid) == []
    asyncio.run(pmf.advance(vid, _runner([]), allow_send=True, idea="x"))
    _age(vid, 80)  # nothing moved
    out = asyncio.run(pmf.advance(vid, _runner([]), allow_send=True, idea="x"))
    assert {i["decision"] for i in out if i["state"] != "running"} == {"kill"}


def test_outreach_experiments_wait_for_a_real_send_instead_of_being_killed(env, monkeypatch):
    client, vid = env
    from app.autopilot import pmf

    m = Metrics(monkeypatch)
    asyncio.run(pmf.advance(vid, _runner([]), allow_send=False, idea="x"))
    asyncio.run(pmf.advance(vid, _runner([]), allow_send=False, idea="x"))
    _age(vid, 100)
    out = asyncio.run(pmf.advance(vid, _runner([]), allow_send=False, idea="x"))
    icp = next(i for i in pmf.journal(vid) if i["variable"] == "icp")
    assert icp["state"] == "running" and icp["waiting_send"] and icp.get("decision") is None
    assert not any(i["variable"] == "icp" and i["state"] != "running" for i in out)
    m.v.update(outbound_touches=5)  # the founder sent them
    asyncio.run(pmf.advance(vid, _runner([]), allow_send=False, idea="x"))
    assert not next(i for i in pmf.journal(vid) if i["variable"] == "icp")["waiting_send"]


def test_service_pmf_pass_runs_through_the_guard_and_posts_the_journal(env, monkeypatch):
    client, vid = env
    from app.autopilot import service

    _on(client, vid)
    m = Metrics(monkeypatch)
    calls = _fake_chat(monkeypatch, [])
    asyncio.run(service.pmf_iterate(vid, None))
    assert [c[0] for c in calls][:2] == ["site.rewrite", "site.publish"]
    m.v.update(waitlist_signups=9)
    _age(vid, 1)
    asyncio.run(service.pmf_iterate(vid, None))
    assert any("PMF journal" in x and "KEEP" in x for x in _msgs(vid))
    assert client.get(f"/ventures/{vid}/autopilot").json()["pmf_journal"]
    client.put(f"/ventures/{vid}/autopilot", json={"god_mode": False})  # off: the guard refuses every inner step
    before = len(client.get(f"/ventures/{vid}/autopilot").json()["pmf_journal"])
    asyncio.run(service.pmf_iterate(vid, None))
    assert len(client.get(f"/ventures/{vid}/autopilot").json()["pmf_journal"]) == before


def test_hand_sent_outreach_and_replies_are_counted(env):
    client, vid = env
    from app.autopilot import pmf

    assert pmf.read_metric(vid, "outbound_touches") == pmf.read_metric(vid, "outbound_replies") == 0
    r = client.post(f"/ventures/{vid}/outreach-log", json={"who": "Owner@Gym.example", "direction": "out", "subject": "hi"})
    assert r.status_code == 201 and (r.json()["sent"], r.json()["replies"]) == (1, 0)
    r = client.post(f"/ventures/{vid}/outreach-log", json={"who": "owner@gym.example", "direction": "in"})
    assert (r.json()["sent"], r.json()["replies"]) == (1, 1)  # same contact, a reply is not a new send
    assert client.post(f"/ventures/{vid}/outreach-log", json={"who": "x", "direction": "sideways"}).status_code == 422


def test_outreach_metric_is_venture_scoped_and_excludes_unattributed_history(env):
    client, a = env
    from app.autopilot import pmf
    from app.db.models import Venture, GtmContact, GtmTouch
    from app.state import db
    from core.clock import utcnow
    b = client.post('/ventures', json={'raw_idea': 'a different owned company'}).json()['id']
    with db()() as session:
        ws = session.get(Venture, a).workspace_id
        session.get(Venture, b).workspace_id = ws
        session.flush()
        now = utcnow()
        session.add(GtmContact(id='scope-contact', workspace_id=ws, created_at=now, stage_changed_at=now))
        session.flush()
        for key, venture, workspace, simulated in [('legacy', None, ws, 0), ('b', b, ws, 0), ('sim', a, ws, 1), ('foreign', a, 'foreign-workspace', 0)]:
            session.add(GtmTouch(id=key, contact_id='scope-contact', venture_id=venture, workspace_id=workspace, direction='in', simulated=simulated, sent_at=now, created_at=now))
        session.commit()
    assert pmf.read_metric(a, 'outbound_replies') == 0
    assert pmf.read_metric(b, 'outbound_replies') == 1
    client.post(f'/ventures/{a}/outreach-log', json={'who': 'shared@example.invalid', 'direction': 'in'})
    assert pmf.read_metric(a, 'outbound_replies') == 1
    assert pmf.read_metric(b, 'outbound_replies') == 1


@pytest.mark.parametrize("old_scoped_baseline", [False, True], ids=["unattributed", "automated-replies-counted"])
def test_legacy_workspace_baseline_is_held_without_remeasurement_or_decision(env, monkeypatch, old_scoped_baseline):
    _, vid = env
    from app.autopilot import pmf
    from core.clock import utcnow
    old = {'n': 1, 'variable': 'icp', 'variant': 0, 'metric': 'outbound_replies', 'hypothesis': 'Legacy experiment',
        'change': 'Legacy change', 'baseline': 99, 'target': 101, 'measured': 100, 'state': 'running', 'decision': None,
        'started_at': (utcnow() - timedelta(hours=100)).isoformat()}
    if old_scoped_baseline:
        old['metric_scope'] = pmf._metric_scope(vid) | {'version': 'venture_touch_v1'}
    async def no_provider(*args):
        return 'Synthetic owned test change'
    monkeypatch.setattr(pmf, 'variant_text', no_provider)
    doc_id = pmf._save(vid, old)
    asyncio.run(pmf.advance(vid, _runner([], ok=False), allow_send=True, idea='no new work'))
    item = next(i for i in pmf.journal(vid) if i['doc_id'] == doc_id)
    assert item['state'] == 'needs_review' and item['metric_qualified'] is False
    assert (item['baseline'], item['measured'], item['decision']) == (99, 100, None)
    assert all(v.name != 'icp' for v, _ in pmf.next_candidates(vid, 'x', [item], 10))


def test_reply_attribution_requires_owned_sent_thread_and_is_idempotent(env):
    _, vid = env
    from app.autopilot import pmf
    from app.db.models import Venture, GtmContact, GtmTouch
    from app.gtm.supymem import pipeline
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        ws = session.get(Venture, vid).workspace_id
        now = utcnow()
        session.add(GtmContact(id='thread-contact', workspace_id=ws, email='thread@example.invalid', created_at=now, stage_changed_at=now))
        session.flush()
        session.add(GtmTouch(id='actual-send', workspace_id=ws, venture_id=vid, provider_thread_id='owned-thread', contact_id='thread-contact', direction='out', simulated=0, sent_at=now, created_at=now))
        session.commit()
    with pytest.raises(ValueError, match='thread_scope_unconfirmed'):
        pipeline.apply_reply('thread-contact', venture_id=vid, provider_thread_id='foreign-thread', provider_message_id='receipt-1')
    assert pmf.read_metric(vid, 'outbound_replies') == 0
    for _ in range(2):
        pipeline.apply_reply('thread-contact', venture_id=vid, provider_thread_id='owned-thread', provider_message_id='receipt-1')
    assert pmf.read_metric(vid, 'outbound_replies') == 1


def test_two_same_workspace_experiments_do_not_share_reply_progress(env, monkeypatch):
    client, a = env
    from app.autopilot import pmf
    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow
    b = client.post('/ventures', json={'raw_idea': 'second local fixture'}).json()['id']
    with db()() as session:
        ws = session.get(Venture, a).workspace_id
        session.get(Venture, b).workspace_id = ws
        session.commit()
    async def no_provider(*args):
        return 'Synthetic test change only'
    monkeypatch.setattr(pmf, 'variant_text', no_provider)
    common = {'n': 1, 'variable': 'icp', 'variant': 0, 'metric': 'outbound_replies', 'hypothesis': 'Owned fixture',
        'change': 'Synthetic fixture change', 'baseline': 0, 'target': 2, 'measured': 0, 'state': 'running', 'decision': None,
        'started_at': (utcnow() - timedelta(hours=100)).isoformat(), 'touches0': 0}
    for vid in (a, b):
        pmf._save(vid, common | {'metric_scope': pmf._metric_scope(vid)})
    pmf.log_touch(b, ws, 'owned-fixture@example.invalid', 'in', logged_by='synthetic reporter')
    for vid in (a, b):
        asyncio.run(pmf.advance(vid, _runner([], ok=False), allow_send=True, idea='no provider'))
    assert pmf.journal(a)[0]['decision'] == 'kill'
    assert pmf.journal(b)[0]['decision'] == 'pivot'
    assert (pmf.journal(a)[0]['measured'], pmf.journal(b)[0]['measured']) == (0, 1)


def test_direct_manual_log_rejects_foreign_workspace_before_touch(env):
    _, vid = env
    from app.autopilot import pmf
    with pytest.raises(ValueError, match='venture_scope_mismatch'):
        pmf.log_touch(vid, 'foreign-workspace', 'synthetic@example.invalid', 'in')
    assert pmf.read_metric(vid, 'outbound_replies') == 0


def test_unknown_thread_owner_cannot_be_inferred_from_matching_contact(env):
    _, vid = env
    from app.autopilot import pmf
    from app.db.models import Venture, GtmContact, GtmTouch
    from app.gtm.supymem import pipeline
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        ws = session.get(Venture, vid).workspace_id
        now = utcnow()
        session.add(GtmContact(id='ambiguous-contact', workspace_id=ws, created_at=now, stage_changed_at=now))
        session.flush()
        for key, owner in [('old-thread-send', None), ('new-thread-send', vid)]:
            session.add(GtmTouch(id=key, workspace_id=ws, venture_id=owner, provider_thread_id='ambiguous-thread', contact_id='ambiguous-contact', direction='out', simulated=0, sent_at=now, created_at=now))
        session.commit()
    with pytest.raises(ValueError, match='thread_scope_ambiguous'):
        pipeline.apply_reply('ambiguous-contact', venture_id=vid, provider_thread_id='ambiguous-thread', provider_message_id='new-reply')
    assert pmf.read_metric(vid, 'outbound_replies') == 0

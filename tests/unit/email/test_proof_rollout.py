"""Targeted progress proof keeps normal rollout and delivery policy intact."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.test_email_engine import env as env, _rows


def held(monkeypatch, people="usr_a"):
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "beta_invite")
    monkeypatch.setenv("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST", people)


def progress(state, *, count=1):
    from app.db.models import ChatMessage, Venture, Workspace
    from app.email import service
    now = service.utcnow()
    with state.db()() as session:
        session.add(Workspace(id="ws_proof_rollout", kind="venture", name="Owned fixture", entry_mode="idea",
                              owner_id="usr_a", created_at=now, updated_at=now))
        session.add(Venture(id="ven_proof_rollout", workspace_id="ws_proof_rollout", name="Owned fixture",
                            raw_idea="Owned fixture", stage="idea", created_at=now, updated_at=now))
        for n in range(count):
            session.add(ChatMessage(id=f"msg_proof_rollout_{n}", venture_id="ven_proof_rollout", role="assistant",
                kind="plan_ready", body_md="The saved plan is ready.", created_at=now - timedelta(hours=3, minutes=n)))
        session.commit()


@pytest.mark.parametrize("flag,allowlist,template,person,expected", [
    (None, None, "milestone", "usr_other", True),
    (None, None, "progress_summary", "usr_a", True),
    (None, None, "progress_summary", "usr_other", False),
    (None, None, "venture_export_prepared", "usr_a", False),
    ("true", None, "progress_summary", "usr_other", True),
    ("true", "beta_invite", "milestone", "usr_a", True),
    (None, "beta_invite", "progress_summary", "usr_a", True),
    ("true", "", "progress_summary", "usr_a", True),
    ("true", "beta_invite", "milestone", "usr_other", False),
    ("true", "beta_invite", "milestone", None, False),
    ("false", "milestone,progress_summary", "milestone", "usr_a", False),
    ("true", "beta_invite", "venture_export_prepared", "usr_a", False),
    ("true", "beta_invite", "error_alert", "usr_a", False),
    ("true", "beta_invite", "beta_invite", "usr_other", True),
    ("true", "milestone", "milestone", "usr_other", True),
])
def test_proof_exception_is_only_held_progress_and_normal_mail_is_unchanged(env, monkeypatch, flag, allowlist, template, person, expected):
    from app.email import service
    held(monkeypatch)
    for key, value in (("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", flag), ("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", allowlist)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert service.template_enabled(template, person) is expected


@pytest.mark.parametrize("count,template", [(1, "milestone"), (2, "progress_summary")])
def test_real_progress_queue_and_send_recheck_person_scope_without_broad_rollout(env, monkeypatch, count, template):
    from app.db.models import Venture
    from app.email import service, triggers
    _, state = env
    held(monkeypatch)
    progress(state, count=count)
    queued = []
    with state.db()() as session:
        triggers._progress(session, session.get(Venture, "ven_proof_rollout"), "usr_a", service.utcnow(),
            lambda owner, name, ctx, ref: queued.append(service.send_email(owner, name, ctx, ref=ref)))
    assert len(queued) == 1 and queued[0]
    row = _rows(state, id=queued[0])[0]
    assert row.template == template and row.to_email == "a@example.test"
    assert service.send_email("usr_b", template, {"proof_person_id": "usr_a"}, ref="unlisted") is None
    monkeypatch.delenv("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST")
    assert service.deliver(row.id) == "deferred"
    assert _rows(state, id=row.id)[0].error == "lifecycle_disabled"
    monkeypatch.setenv("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST", "usr_a")
    assert service.deliver(row.id) == service.deliver(row.id) == "sent"
    assert service.send_email("usr_a", template, {}, ref="daily-cap") is None
    with state.db()() as session:
        assert session.scalar(select(type(row).id).where(type(row).template == template)) == row.id


def test_targeted_queue_requires_real_canonical_person_and_recipient(env, monkeypatch):
    from app.db.models import Person
    from app.email import service
    _, state = env
    held(monkeypatch, "usr_a,usr_missing")
    assert service.send_email("usr_missing", "milestone", {}, to_email="a@example.test", ref="missing") is None
    assert service.send_email("usr_a", "milestone", {}, to_email="foreign@example.test", ref="foreign") is None
    progress(state)
    mid = service.send_email("usr_a", "milestone", {"what": "plan", "path": "/v/ven_proof_rollout/plan"}, ref="bound")
    with state.db()() as session:
        session.get(Person, "usr_a").email = "new@example.test"
        session.commit()
    assert service.deliver(mid) == "suppressed"
    assert _rows(state, id=mid)[0].error == "recipient_changed"


def test_empty_proof_allowlist_leaves_held_templates_off(env, monkeypatch):
    from app.email import service
    held(monkeypatch, "")
    for template in ("milestone", "progress_summary"):
        assert not service.template_enabled(template, "usr_a")
        assert service.send_email("usr_a", template, {}, ref="held") is None
    assert service.template_enabled("beta_invite", "usr_a")
    monkeypatch.delenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST")
    monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED")
    assert not service.template_enabled("progress_summary", "usr_a")
    assert service.template_enabled("milestone", "usr_a")  # Original default is unchanged.


@pytest.mark.parametrize("guard", ["global_stop", "opt_out", "suppression", "quiet"])
def test_targeted_queue_keeps_existing_stop_consent_suppression_and_quiet_guards(env, monkeypatch, guard):
    from app.db.models import AnalyticsEvent
    from app.email import service
    _, state = env
    held(monkeypatch)
    if guard == "global_stop":
        monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "false")
    elif guard == "opt_out":
        service.set_prefs("usr_a", {"product_updates": False})
    elif guard == "suppression":
        service.suppress("a@example.test", "hard_bounce")
    else:
        with state.db()() as session:
            session.add(AnalyticsEvent(id="ane_proof_active", person_id="usr_a", event_name="session.active",
                                      occurred_at=service.utcnow(), props_json="{}"))
            session.commit()
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="guarded")
    if guard in {"global_stop", "quiet"}:
        assert mid is None and not _rows(state)
    else:
        assert service.deliver(mid) == "suppressed"
        assert _rows(state, id=mid)[0].error == ("opted_out" if guard == "opt_out" else "address_suppressed")


@pytest.mark.parametrize("guard", ["global_stop", "opt_out", "quiet"])
def test_queued_targeted_mail_rechecks_normal_policy_before_dispatch(env, monkeypatch, guard):
    from app.db.models import AnalyticsEvent
    from app.email import service
    _, state = env
    held(monkeypatch)
    progress(state)
    mid = service.send_email("usr_a", "milestone", {"what": "plan", "path": "/v/ven_proof_rollout/plan"}, ref="queued")
    if guard == "global_stop":
        monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "false")
    elif guard == "opt_out":
        service.set_prefs("usr_a", {"product_updates": False})
    else:
        with state.db()() as session:
            session.add(AnalyticsEvent(id="ane_proof_active", person_id="usr_a", event_name="ui.screen_view",
                                      occurred_at=service.utcnow(), props_json="{}"))
            session.commit()
    assert service.deliver(mid) == ("suppressed" if guard == "opt_out" else "deferred")
    assert _rows(state, id=mid)[0].sent_at is None


def test_status_describes_scoped_exception_without_disclosing_person_ids(env, monkeypatch):
    from app.email import service
    held(monkeypatch)
    rollout = service.status()["rollout"]
    assert rollout["proof_templates"] == ["milestone", "progress_summary"]
    assert rollout["proof_person_allowlist_configured"] is True
    assert "milestone" in rollout["disabled_templates"] and "beta_invite" in rollout["enabled_templates"]
    assert "usr_a" not in str(rollout)

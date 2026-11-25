"""Synthetic standing-report records, with no model, email, or provider calls."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def reports_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.db.models import Base, GtmContact, GtmTouch, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import Task, TaskRun
    from app.tasks import reports
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, VentureDocument, Task, TaskRun, VentureWaitlistEntry, GtmContact, GtmTouch)])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(Person(id="usr_reports", display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_reports", kind="company", name="Synthetic workspace", entry_mode="idea",
            owner_id="usr_reports", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_reports", workspace_id="ws_reports", name="Synthetic venture",
            raw_idea="Not live data", stage="draft", created_at=now, updated_at=now))
        session.add(Task(id="task_reports", workspace_id="ws_reports", venture_id="ven_reports",
            owner_person_id="usr_reports", title="Weekly summary", goal="Summarize waitlist sign-ups and replies",
            kind="scheduled", state="working", deadline_at=now + timedelta(minutes=5),
            created_at=now, updated_at=now))
        session.flush()
        session.add(TaskRun(id="run_reports", task_id="task_reports", run_key="synthetic-first",
            state="working", started_at=now, deadline_at=now + timedelta(minutes=5)))
        session.commit()
    yield {"reports": reports, "factory": factory, "now": now}
    engine.dispose()


def _signup(d, name, when, *, workspace="ws_reports", venture="ven_reports", is_test=False):
    from app.db.supymem_models import VentureWaitlistEntry
    with d["factory"]() as session:
        session.add(VentureWaitlistEntry(id="signup_" + name, workspace_id=workspace, venture_id=venture,
            email=name + "@example.test", consent_version="synthetic", consent_text="Synthetic consent",
            is_test=is_test, created_at=when))
        session.commit()


def _reply(d, name, when, *, workspace="ws_reports", venture="ven_reports", contact_workspace=None,
           classification="human_neutral", simulated=0, direction="in", attributed=True, sent=True):
    from app.db.models import GtmContact, GtmTouch
    with d["factory"]() as session:
        contact = GtmContact(id="contact_" + name, workspace_id=contact_workspace or workspace,
            email=name + "@example.test", source="synthetic", created_at=when, stage_changed_at=when)
        session.add(contact)
        session.flush()
        if sent:
            session.add(GtmTouch(id="sent_" + name, workspace_id=workspace, venture_id=venture,
                contact_id=contact.id, direction="out", simulated=0, provider_thread_id="thread_" + name,
                sent_at=when - timedelta(days=1), created_at=when - timedelta(days=1)))
        session.add(GtmTouch(id="reply_" + name, workspace_id=workspace, venture_id=venture,
            contact_id=contact.id, direction=direction, simulated=simulated, classification=classification,
            channel="email", provider_thread_id="thread_" + name if attributed else None,
            message_id="message_" + name if attributed else None, subject="Private synthetic subject",
            body_text="Private synthetic reply contents", created_at=when))
        session.commit()


def _write(d, *, run_id="run_reports", now=None):
    with d["factory"]() as session:
        result = d["reports"].write_report(session, task_id="task_reports", run_id=run_id,
            workspace_id="ws_reports", venture_id="ven_reports", now=now or d["now"])
        session.commit()
        return result


def _verify(d, *, run_id="run_reports", workspace_id="ws_reports", venture_id="ven_reports"):
    with d["factory"]() as session:
        return d["reports"].verify_report(session, task_id="task_reports", run_id=run_id,
            workspace_id=workspace_id, venture_id=venture_id)


def _document(d, run_id="run_reports"):
    from app.db.models import VentureDocument
    with d["factory"]() as session:
        return session.get(VentureDocument, "task-report:" + run_id)


def _next_run(d, *, run_id="run_next", when=None, prior_state="completed"):
    from app.db.task_models import TaskRun
    when = when or d["now"] + timedelta(days=7)
    with d["factory"]() as session:
        prior = session.get(TaskRun, "run_reports")
        prior.state, prior.finished_at = prior_state, d["now"]
        session.add(TaskRun(id=run_id, task_id="task_reports", run_key="synthetic:" + run_id,
            state="working", started_at=when, deadline_at=when + timedelta(minutes=5)))
        session.commit()
    return when


def test_report_counts_scoped_real_signups_and_attributed_human_replies(reports_db):
    d, now = reports_db, reports_db["now"]
    _signup(d, "included", now - timedelta(days=1))
    _signup(d, "test_probe", now - timedelta(days=1), is_test=True)
    _signup(d, "foreign_workspace", now - timedelta(days=1), workspace="foreign")
    _signup(d, "foreign_venture", now - timedelta(days=1), venture="foreign")
    _signup(d, "too_old", now - timedelta(days=8))
    for name, kind in ("positive", "human_positive"), ("negative", "human_negative"), ("neutral", "human_neutral"):
        _reply(d, name, now - timedelta(days=1), classification=kind)
    for name, options in (
        ("automated", {"classification": "automated"}), ("simulated", {"simulated": 1}),
        ("unattributed", {"attributed": False}), ("unsent", {"sent": False}),
        ("foreign_workspace", {"workspace": "foreign"}), ("foreign_venture", {"venture": "foreign"}),
        ("foreign_contact", {"contact_workspace": "foreign"}), ("outgoing", {"direction": "out"}),
    ):
        _reply(d, name, now - timedelta(days=1), **options)
    result = _write(d)
    assert result["verified"] and not result["quiet"]
    assert result["evidence"][0]["signup_count"] == 1 and result["evidence"][0]["reply_count"] == 3
    data = json.loads(_document(d).provenance_json)
    assert data["snapshot"]["counts"] == {"signup_count": 1, "reply_count": 3,
        "interested_reply_count": 1, "negative_reply_count": 1, "neutral_reply_count": 1}
    assert data["snapshot"]["source_refs"] == {"waitlist_entry_ids": ["signup_included"],
        "reply_touch_ids": ["reply_negative", "reply_neutral", "reply_positive"]}
    assert _verify(d)["verified"]


def test_artifact_and_timeline_evidence_contain_no_contact_details(reports_db):
    d = reports_db
    _signup(d, "private", d["now"] - timedelta(hours=1))
    _reply(d, "private", d["now"] - timedelta(hours=1))
    result = _write(d)
    row = _document(d)
    serialized = row.body_md + row.provenance_json + json.dumps(result)
    assert "@example.test" not in serialized and "Private synthetic" not in serialized
    assert "contact_private" not in serialized and "message_private" not in serialized
    assert "task_reports" not in row.body_md and "run_reports" not in row.body_md


def test_window_is_half_open_and_exactly_seven_days_initially(reports_db):
    d, now = reports_db, reports_db["now"]
    _signup(d, "at_start", now - timedelta(days=7))
    _signup(d, "before_start", now - timedelta(days=7, microseconds=1))
    _signup(d, "at_end", now)
    _signup(d, "future", now + timedelta(seconds=1))
    result = _write(d)
    assert result["evidence"][0]["signup_count"] == 1
    assert json.loads(_document(d).provenance_json)["window"] == {
        "start_at": (now - timedelta(days=7)).isoformat(), "end_at": now.isoformat()}


def test_next_summary_starts_at_previous_successful_report_end(reports_db):
    d, now = reports_db, reports_db["now"]
    _signup(d, "reported", now - timedelta(hours=1))
    _write(d)
    next_at = _next_run(d, when=now + timedelta(days=3))
    _signup(d, "boundary", now)
    _signup(d, "new", now + timedelta(days=1))
    result = _write(d, run_id="run_next", now=next_at)
    assert result["evidence"][0]["signup_count"] == 2
    assert json.loads(_document(d, "run_next").provenance_json)["window"]["start_at"] == now.isoformat()
    assert _verify(d, run_id="run_next")["verified"]


def test_old_previous_report_does_not_expand_window_past_seven_days(reports_db):
    d, now = reports_db, reports_db["now"]
    _write(d)
    next_at = _next_run(d, when=now + timedelta(days=30))
    _signup(d, "outside_cap", now + timedelta(days=10))
    _signup(d, "within_cap", now + timedelta(days=29))
    result = _write(d, run_id="run_next", now=next_at)
    assert result["evidence"][0]["signup_count"] == 1
    assert json.loads(_document(d, "run_next").provenance_json)["window"]["start_at"] == (next_at - timedelta(days=7)).isoformat()


def test_failed_run_does_not_advance_summary_watermark(reports_db):
    d, now = reports_db, reports_db["now"]
    _signup(d, "unreported", now - timedelta(days=1))
    _write(d)
    next_at = _next_run(d, when=now + timedelta(days=2), prior_state="failed")
    result = _write(d, run_id="run_next", now=next_at)
    assert result["evidence"][0]["signup_count"] == 1
    assert json.loads(_document(d, "run_next").provenance_json)["window"]["start_at"] == (next_at - timedelta(days=7)).isoformat()


def test_same_run_replay_recovers_frozen_report_without_duplicate_artifact(reports_db):
    from app.db.models import VentureDocument
    d = reports_db
    _signup(d, "original", d["now"] - timedelta(hours=1))
    first = _write(d)
    _signup(d, "after_snapshot", d["now"] + timedelta(seconds=1))
    assert _write(d, now=d["now"] + timedelta(days=1)) == first
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(VentureDocument)) == 1


def test_empty_standing_runs_are_quiet_even_after_nonempty_result(reports_db):
    d = reports_db
    _signup(d, "last_week", d["now"] - timedelta(hours=1))
    assert not _write(d)["quiet"]
    next_at = _next_run(d)
    result = _write(d, run_id="run_next", now=next_at)
    assert result["verified"] and result["quiet"] and result["evidence"][0]["signup_count"] == 0


def test_one_time_empty_report_still_returns_a_result(reports_db):
    from app.db.task_models import Task
    d = reports_db
    with d["factory"]() as session:
        session.get(Task, "task_reports").kind = "one_time"
        session.commit()
    assert _write(d)["verified"] and not _write(d)["quiet"]


def test_empty_outcome_digest_ignores_advancing_window(reports_db):
    d = reports_db
    first = _write(d)
    next_at = _next_run(d)
    second = _write(d, run_id="run_next", now=next_at)
    assert first["payload_digest"] == second["payload_digest"] and first["quiet"] and second["quiet"]


@pytest.mark.parametrize("mutation", ["counts", "source_refs", "body", "window", "scope", "quiet"])
def test_verifier_rejects_tampered_or_changed_snapshot(reports_db, mutation):
    from app.db.models import VentureDocument
    d = reports_db
    _signup(d, "one", d["now"] - timedelta(hours=1))
    _write(d)
    with d["factory"]() as session:
        row = session.get(VentureDocument, "task-report:run_reports")
        data = json.loads(row.provenance_json)
        if mutation == "counts":
            data["snapshot"]["counts"]["signup_count"] = 2
        elif mutation == "source_refs":
            data["snapshot"]["source_refs"]["waitlist_entry_ids"] = ["foreign"]
        elif mutation == "body":
            row.body_md = "Everything is great."
        elif mutation == "window":
            data["window"]["start_at"] = (d["now"] - timedelta(days=8)).isoformat()
        elif mutation == "scope":
            data["workspace_id"] = "foreign"
        else:
            data["quiet"] = True
        row.provenance_json = json.dumps(data)
        session.commit()
    assert not _verify(d)["verified"]
    with pytest.raises(ValueError, match="task_report_changed"):
        _write(d)


def test_verifier_rechecks_authoritative_count_in_saved_window(reports_db):
    from app.db.supymem_models import VentureWaitlistEntry
    d = reports_db
    _signup(d, "one", d["now"] - timedelta(hours=1))
    _write(d)
    with d["factory"]() as session:
        session.get(VentureWaitlistEntry, "signup_one").is_test = True
        session.commit()
    assert not _verify(d)["verified"]


def test_report_rejects_foreign_scope_and_missing_artifact(reports_db):
    d = reports_db
    assert not _verify(d)["verified"]
    with d["factory"]() as session, pytest.raises(PermissionError, match="scope_mismatch"):
        d["reports"].write_report(session, task_id="task_reports", run_id="run_reports",
            workspace_id="foreign", venture_id="ven_reports", now=d["now"])
    assert _verify(d, venture_id="foreign")["blocker"]["class"] == "policy"

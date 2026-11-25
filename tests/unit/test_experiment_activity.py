"""Option receipts stay truthful for unrelated business and consumer audiences."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.unit.test_autonomous_experiments import market, venture


@pytest.fixture
def activity_case(market, monkeypatch):
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings
        from app.db.models import Venture
        from app.state import db
        from app.ventures import founder_facts
        from tests.unit.gtm.test_journey import _mail

        _mail({"vid": market[1], "wid": market[2], "factory": db(), "monkeypatch": monkeypatch})
        update_settings("capabilities", {"send_email": True}, {})
        with db()() as session:
            venture = session.get(Venture, market[1])
            founder_facts.save(session, venture, {field: founder_facts._fact(value, source="review", confidence=1.0)
                for field, value in (("audience", market[3]["icp"]["who"]), ("problem", market[3]["icp"]["problem"]))})
            session.commit()
    return {"option_id": "option_a", "venture_id": market[1], "workspace_id": market[2]}, market[3]["icp"]["who"]


def test_founder_evidence_read_does_not_start_a_nested_writer(market):
    from sqlalchemy import select
    from app.autopilot.claims import founder_words
    from app.chat.conversations import bind_conversation
    from app.chat.store import list_messages, page_messages
    from app.db.models import ChatConversation, ChatMessage, Venture, Workspace
    from app.db.session import serialize_writes
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow

    _client, _vid, workspace_id, view, *_rest = market
    audience = view["icp"]["who"]
    venture_id = "read_only_evidence"
    with db()() as session:
        owner = session.get(Workspace, workspace_id).owner_id
        session.add(Venture(id=venture_id, workspace_id=workspace_id, name=audience,
            raw_idea=audience, stage="idea", created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(ChatConversation(id="private_evidence", venture_id=venture_id, workspace_id=workspace_id,
            creator_person_id=owner, visibility="private", title=audience, creation_key="private_evidence",
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(ChatMessage(id="private_statement", venture_id=venture_id, workspace_id=workspace_id,
            conversation_id="private_evidence", role="user", body_md="Private notes about " + audience, created_at=utcnow()))
        session.commit()
    with db()() as writer:
        serialize_writes(writer, key="outreach:" + venture_id)
        assert founder_words(venture_id) == audience
        assert list_messages(venture_id) == []
        assert page_messages(venture_id) == ([], None)
        with pytest.raises(ValueError, match="chat_conversation_not_found"):
            list_messages(venture_id, conversation_id="another_conversation")
        assert writer.scalar(select(ChatConversation.id).where(ChatConversation.creation_key == "legacy:" + venture_id)) is None
        with bind(workspace_id=workspace_id, venture_id=venture_id, actor=owner):
            with bind_conversation(venture_id, "private_evidence"):
                assert founder_words(venture_id) == audience + "\nPrivate notes about " + audience
        with bind(workspace_id=workspace_id, venture_id=venture_id, actor="another_person"):
            with pytest.raises(ValueError, match="chat_conversation_not_found"):
                with bind_conversation(venture_id, "private_evidence"):
                    pytest.fail("Your private conversation must remain private.")


def test_chat_next_steps_do_not_hold_a_writer_while_reading_prerequisites(market, monkeypatch):
    from app.chat import next_moves
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.db.session import serialize_writes
    from app.state import db

    venture_id = market[1]
    calls = []

    def attach(venture_id, row, **kwargs):
        with db()() as session:
            session.execute(text("PRAGMA busy_timeout=25"))
            serialize_writes(session, key="prerequisite-evidence:" + venture_id)
            session.commit()
        calls.append(venture_id)
        row.actions_json = json.dumps([{"label": "Read your notes", "action": "status"}])

    monkeypatch.setattr(next_moves, "attach", attach)
    result = append_message(venture_id, role="assistant", body_md="Your notes cover " + market[3]["icp"]["who"])
    assert calls == [venture_id]
    with db()() as session:
        assert json.loads(session.get(ChatMessage, result["id"]).actions_json)[0]["label"] == "Read your notes"


@pytest.mark.parametrize("change", ["remove", "edit"])
def test_chat_next_steps_preserve_your_removed_or_edited_reply(market, monkeypatch, change):
    from sqlalchemy import delete
    from app.chat import next_moves
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.state import db

    changed = []
    replacement = "Your revised notes cover " + market[3]["icp"]["who"]

    def attach(venture_id, row, **kwargs):
        with db()() as session:
            if change == "remove":
                session.execute(delete(ChatMessage).where(ChatMessage.id == row.id))
            else:
                session.get(ChatMessage, row.id).body_md = replacement
            session.commit()
        changed.append(row.id)

    monkeypatch.setattr(next_moves, "attach", attach)
    if change == "remove":
        with pytest.raises(ValueError, match="send your request again"):
            append_message(market[1], role="assistant", body_md="Your notes cover " + market[3]["icp"]["who"])
    else:
        result = append_message(market[1], role="assistant", body_md="Your notes cover " + market[3]["icp"]["who"])
        assert replacement in result["body_md"]
    with db()() as session:
        assert changed
        saved = session.get(ChatMessage, changed[0])
        assert saved is None if change == "remove" else saved.body_md == replacement


def test_append_only_receipts_preserve_real_instants_and_scope(activity_case):
    from app.db.models import ActivityLine
    from app.experiments import activity
    from app.state import db

    context, audience = activity_case
    started = datetime(2026, 10, 27, 9, 3, 2, 456789, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    ended = started + timedelta(seconds=17, microseconds=83)
    identifier = activity.record(context, action="search", tool="firecrawl", evidence_level="desk_research",
        evidence_summary=audience, started_at=started, ended_at=ended)
    with db()() as session:
        session.add(ActivityLine(venture_id=context["venture_id"], phase="experiments", kind="narration", status="done",
            text=f"Unverified narration about {audience}", detail_json=json.dumps({"option_id": context["option_id"]}), ts=ended))
        session.commit()
    item, = activity.list_activity(context["venture_id"], context["option_id"])
    assert datetime.fromisoformat(item["started_at"]) == started
    assert datetime.fromisoformat(item["ended_at"]) == ended
    assert item["text"] == f"Searched {audience}"
    assert activity.list_activity("another_venture", context["option_id"]) == []
    assert activity.list_activity(context["venture_id"], "another_option") == []
    with db()() as session:
        for statement in ("UPDATE activity_lines SET detail_json='invented' WHERE seq=:id",
                          "DELETE FROM activity_lines WHERE seq=:id"):
            with pytest.raises(IntegrityError, match="append-only"):
                session.execute(text(statement), {"id": identifier})
            session.rollback()
    assert activity.list_activity(context["venture_id"])[0] == item


def test_activity_validates_scope_level_interval_counts_and_source(activity_case):
    from app.experiments import activity

    context, audience = activity_case
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    values = dict(action="search", tool="firecrawl", evidence_level="desk_research",
        evidence_summary=audience, started_at=now, ended_at=now)
    for overrides in ({"evidence_level": "proof_of_demand"}, {"ended_at": now - timedelta(seconds=1)},
                      {"source_url": "http://127.0.0.1/private"}, {"action": "email", "evidence_summary": "invented"}):
        with pytest.raises(ValueError):
            activity.record(context, **(values | overrides))
    with pytest.raises(ValueError, match="scope_invalid"):
        activity.record(context | {"workspace_id": "foreign_workspace"}, **values)
    assert not activity.list_activity(context["venture_id"])


def test_no_option_does_no_storage_or_validation(activity_case, monkeypatch):
    from app.experiments import activity

    def forbidden():
        raise AssertionError("No-option runs must not open storage")

    monkeypatch.setattr(activity, "db", forbidden)
    context, audience = activity_case
    context = {key: value for key, value in context.items() if key != "option_id"}
    context["spec"] = None
    assert activity.record(context, action="search", tool="firecrawl", evidence_level="not_a_level",
        started_at=None, ended_at=None, evidence_summary=audience) is None
    assert activity.record_safely(context, action="search", tool="firecrawl", evidence_level="not_a_level",
        started_at=None, ended_at=None, evidence_summary=audience) is None
    with activity.track(context, action="search", tool="firecrawl", evidence_level="desk_research"):
        pass
    with pytest.raises(RuntimeError, match="original_error"):
        with activity.track(context, action="search", tool="firecrawl", evidence_level="desk_research"):
            raise RuntimeError("original_error")


def test_parallel_and_sequential_runner_intervals_are_not_rewritten(activity_case, market):
    from app.experiments import activity, autonomous

    context, audience = activity_case
    spec = next(item for item in autonomous.available(context["venture_id"]) if item["scan"] == "demand")
    spec = spec | context | {"queries": [audience], "broader_queries": [], "variants": [], "pages": 0}

    async def execute():
        entered, release = asyncio.Event(), asyncio.Event()
        starts = 0

        async def search(query, **kwargs):
            nonlocal starts
            starts += 1
            if starts == 2:
                entered.set()
            await release.wait()
            return []

        provider = SimpleNamespace(name="firecrawl", search=search)
        first = asyncio.create_task(autonomous._scan(spec | {"option_id": "parallel_a"}, provider))
        second = asyncio.create_task(autonomous._scan(spec | {"option_id": "parallel_b"}, provider))
        await entered.wait()
        release.set()
        await asyncio.gather(first, second)
        await autonomous._scan(spec | {"option_id": "sequential_a"}, provider)
        await autonomous._scan(spec | {"option_id": "sequential_b"}, provider)

    asyncio.run(execute())
    items = {item["option_id"]: item for item in activity.list_activity(context["venture_id"])}
    interval = lambda key: tuple(datetime.fromisoformat(items[key][name]) for name in ("started_at", "ended_at"))
    first_start, first_end = interval("parallel_a")
    second_start, second_end = interval("parallel_b")
    assert max(first_start, second_start) < min(first_end, second_end)
    assert interval("sequential_a")[1] <= interval("sequential_b")[0]
    assert all(item["text"] == f"Searched {audience}" and item["evidence_level"] == "desk_research" for item in items.values())


def test_scan_receipts_include_only_attempted_tools_and_safe_failures(activity_case, market):
    from app.core.local_mode import is_local
    from app.experiments import activity, autonomous

    context, audience = activity_case
    spec = next(item for item in autonomous.available(context["venture_id"]) if item["scan"] == "demand")
    spec = spec | context | {"queries": [audience, "unavailable search"], "broader_queries": [], "variants": [], "pages": 1}

    async def search(query, **kwargs):
        if query == "unavailable search":
            raise RuntimeError("private provider token")
        return [{"url": "https://discussion.example/source?utm_campaign=test&token=private", "title": audience}]

    async def scrape(url):
        return "   "

    asyncio.run(autonomous._scan(spec, SimpleNamespace(name="firecrawl", search=search, scrape=scrape)))
    items = activity.list_activity(context["venture_id"])
    assert [item["action"] for item in items] == ["search", "search", "fetch"]
    assert [item["outcome"] for item in items] == ["succeeded", "failed", "failed" if is_local() else "empty"]
    assert items[-1]["source_url"] == "https://discussion.example/source"
    assert ("Could not read" if is_local() else "No readable content") in items[-1]["text"]
    assert "private" not in json.dumps(items)


def test_renderer_uses_plain_words_not_internal_levels(activity_case):
    from app.experiments import activity

    _context, audience = activity_case
    for action, summary, expected in (("search", audience, f"Searched {audience}"),
            ("email", "12", "Emailed 12 people"), ("reply", "2", "2 replied"),
            ("draft", "2", "Prepared 2 email drafts"), ("discover", "3", "Found 3 potential contacts"),
            ("compare", "", "Checked your results")):
        label = activity.render({"action": action, "outcome": "succeeded", "evidence_summary": summary,
            "evidence_level": "hypothesis"})
        assert label == expected
        assert not any(level.value in label for level in activity.EvidenceLevel)
        assert "\u2014" not in label
    assert activity.render({"action": "email", "outcome": "unconfirmed", "evidence_summary": "12"}) == "Your email delivery was not confirmed"


def test_failed_activity_storage_does_not_replace_provider_error(activity_case, monkeypatch, caplog):
    from app.experiments import activity

    context, audience = activity_case
    def unavailable(*args, **kwargs):
        raise ValueError("storage unavailable")

    monkeypatch.setattr(activity, "record", unavailable)
    with pytest.raises(TimeoutError, match="provider deadline"):
        with activity.track(context, action="search", tool="firecrawl", evidence_level="desk_research", evidence_summary=audience):
            raise TimeoutError("provider deadline")
    with activity.track(context, action="search", tool="firecrawl", evidence_level="desk_research", evidence_summary=audience):
        pass
    failures = [record for record in caplog.records if getattr(record, "action", None) == "experiment.activity.write"]
    assert len(failures) == 2 and all(record.error_type == "ValueError" for record in failures)
    assert "storage unavailable" not in caplog.text and "provider deadline" not in caplog.text


def test_research_fetches_log_real_calls_not_receipt_replays(activity_case, monkeypatch):
    from app.experiments import activity
    from app.research import deep

    context, audience = activity_case
    calls, cached = [], {}

    def retrieve(did, key, payload, **kwargs):
        if key not in cached:
            cached[key] = kwargs["callback"]()
        return cached[key]

    async def search(query):
        calls.append(("search", query))
        return []

    async def scrape(url):
        calls.append(("fetch", url))
        return audience

    monkeypatch.setattr(deep, "_retrieve", retrieve)
    provider = SimpleNamespace(name="firecrawl", search=search, scrape=scrape)
    for _repeat in range(2):
        deep._search("dossier", context, provider, "direct", audience, audience)
        deep._scrape("dossier", context, provider, {"id": "source_123", "url": "https://source.example/page"})
    assert len(calls) == 2
    items = activity.list_activity(context["venture_id"])
    assert [item["action"] for item in items] == ["search", "fetch"]
    assert items[-1]["evidence_summary"] == audience
    assert all(item["evidence_level"] == "desk_research" for item in items)


def test_waitlist_email_logs_only_confirmed_delivery_with_actual_times(activity_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import activity, page_tests
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    from app.state import db
    from core.clock import utcnow

    context, audience = activity_case
    now = utcnow()
    draft = {"doc_id": "draft_activity", "experiment_id": "experiment_activity", "to": "consented@example.org",
        "waitlist_entry_id": "consented_entry", "subject": audience, "body": "Your approved compliant body", "credit_ceiling_micro": 1}
    with db()() as session:
        session.add(Experiment(id=draft["experiment_id"], venture_id=context["venture_id"], workspace_id=context["workspace_id"], kind="market_test", context=json.dumps({"spec": context})))
        session.add(VentureDocument(id=draft["doc_id"], venture_id=context["venture_id"], kind=outreach.KIND,
            title=audience, body_md=draft["body"], provenance_json=json.dumps(draft), created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(page_tests, "eligible", lambda *_args: True)
    monkeypatch.setattr("app.approvals.external.outbound_rate", lambda *_args: SimpleNamespace(amount_micro=1))
    delivery = {"provider_message_id": "confirmed_message"}
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: delivery)

    def meter(invoke, **kwargs):
        receipt = invoke()
        kwargs["before_meter_exit"](receipt, {"actual_micro": 1})
        return receipt

    monkeypatch.setattr("app.credits.metered.call_sync", meter)
    timestamps = iter((now, now + timedelta(seconds=3), now + timedelta(seconds=4), now + timedelta(seconds=5)))
    monkeypatch.setattr(page_tests, "utcnow", lambda: next(timestamps))
    assert page_tests.send_one(context["venture_id"], context["workspace_id"], draft, {}) == delivery
    item, = activity.list_activity(context["venture_id"])
    assert item["text"] == "Emailed 1 person" and item["evidence_level"] == "sent_outreach"
    assert datetime.fromisoformat(item["started_at"]) == now
    assert datetime.fromisoformat(item["ended_at"]) == now + timedelta(seconds=3)
    delivery["provider_message_id"] = "sim_unconfirmed"
    with pytest.raises(RuntimeError, match="delivery_unconfirmed"):
        page_tests.send_one(context["venture_id"], context["workspace_id"], draft, {})
    monkeypatch.setattr(page_tests, "eligible", lambda *_args: False)
    with pytest.raises(DefiniteFailure, match="Nothing was sent"):
        page_tests.send_one(context["venture_id"], context["workspace_id"], draft, {})
    assert len(activity.list_activity(context["venture_id"])) == 1


@pytest.mark.parametrize("failure", ["validation", "database", "context_lookup", "interrupted"])
def test_activity_failures_do_not_rollback_sent_outreach(activity_case, market, monkeypatch, caplog, failure):
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.db.models import ActivityLine, GtmContact, GtmTouch, VentureDocument
    from app.experiments import activity
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select
    from tests.unit.autopilot.test_outreach import _contacts, _mailbox, _on

    context, audience = activity_case
    venture_id = context["venture_id"]
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "1")
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": True}, {})
    _mailbox(venture_id)
    _on(market[0], venture_id, allow_send=True, send_max_per_day=1)
    contact_id, = _contacts(venture_id, 1, domain="public")
    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        contact.dossier_hook = f"Public contact for {audience}"
        contact.enrichment_json = json.dumps({"business": audience, "found_at": "https://public0.example/contact"})
        session.commit()

    async def no_model(*args, **kwargs):
        return {}

    monkeypatch.setattr(outreach, "_llm_drafts", no_model)
    monkeypatch.setattr(outreach, "saved_pitch", lambda _venture: f"A tool for {audience}")
    asyncio.run(outreach.draft_batch(venture_id, review_only=True))
    draft, = outreach.drafts(venture_id, "draft")
    sent, attempts = [], []

    def deliver(*args):
        sent.append(args)
        return {"provider_message_id": "confirmed_activity_send", "thread_id": "activity_thread"}

    def fail_context(_session, *_args):
        attempts.append(len(outreach.drafts(venture_id, "sent")))
        if failure == "context_lookup":
            raise RuntimeError("activity context lookup failed")
        return context

    def fail_record(_context, *, session, **fields):
        if failure == "validation":
            raise ValueError("activity validation failed")
        if failure == "interrupted":
            raise KeyboardInterrupt("activity write interrupted")
        session.add(ActivityLine(venture_id=venture_id, phase="experiments", kind="option_activity",
            status="failed", text=None, ts=utcnow()))
        session.flush()

    monkeypatch.setattr(outreach, "_send_one", deliver)
    monkeypatch.setattr(activity, "experiment_context", fail_context)
    monkeypatch.setattr(activity, "record", fail_record)
    with db()() as session:
        row = session.get(VentureDocument, draft["doc_id"])
        row.title = audience
        assert activity.record_safely(lambda target: fail_context(target), session=session) is None
        session.commit()
    approval = {"approved_by_founder": True, "batch_id": draft["batch_id"]}
    asyncio.run(outreach.send_batch(venture_id, approval))
    with db()() as session:
        saved = session.get(VentureDocument, draft["doc_id"])
        assert saved.title == audience and json.loads(saved.provenance_json)["state"] == "sent"
        touch, = session.scalars(select(GtmTouch).where(GtmTouch.venture_id == venture_id, GtmTouch.direction == "out"))
        assert touch.contact_id == contact_id and touch.message_id == "confirmed_activity_send" and touch.simulated == 0
    asyncio.run(outreach.send_batch(venture_id, approval))
    assert len(sent) == 1
    assert attempts == [0, 1]
    assert any(entry.levelname == "ERROR" and entry.message == "founder_action_failed" for entry in caplog.records)


def test_page_and_channel_readouts_record_comparisons_not_demand(activity_case, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.experiments import activity, channel_tests, ledger, page_tests
    from app.state import db
    from core.clock import utcnow

    context, audience = activity_case
    now = utcnow()
    for runner, scan in ((page_tests, "page_test"), (channel_tests, "channel_test")):
        identifier = "experiment_" + scan
        spec = context | {"id": identifier, "scan": scan, "page_kind": "survey", "max_duration_days": 14,
            "variants": [{"key": "first", "label": audience}]}
        if scan == "channel_test":
            spec["channel_kind"] = "channels"
        with db()() as session:
            spec["direction"] = ledger.current_direction(session, context["venture_id"])
            session.add(Experiment(id=identifier, venture_id=context["venture_id"], workspace_id=context["workspace_id"],
                kind="market_test", state="running", context=json.dumps({"spec": spec, "prepared_at": now.isoformat()})))
            session.commit()
        monkeypatch.setattr(runner, "observe", lambda _spec: {"outcome": "inconclusive", "insight": audience, "sources": []})
        runner.readout(context["venture_id"], {"experiment_id": identifier})
    items = activity.list_activity(context["venture_id"])
    assert len(items) == 2
    assert all(item["evidence_level"] == "conclusion" and item["text"] == "Checked your results" for item in items)
    assert all(item["outcome"] == "inconclusive" for item in items)


def test_reply_activity_excludes_automated_and_ambiguous_attribution(activity_case):
    from app.db.models import GtmContact, GtmTouch
    from app.db.supymem_models import Experiment
    from app.experiments import activity, outreach_tests
    from app.state import db
    from core.clock import utcnow

    context, audience = activity_case
    now = utcnow()
    with db()() as session:
        session.add(GtmContact(id="contact_activity", workspace_id=context["workspace_id"], email="consented@example.org",
            source="venture:" + context["venture_id"], created_at=now, stage_changed_at=now))
        for index in range(3):
            identifier = f"experiment_reply_{index}"
            session.add(Experiment(id=identifier, venture_id=context["venture_id"], workspace_id=context["workspace_id"],
                kind="market_test", context=json.dumps({"spec": context | {"option_id": f"option_{index}"}})))
            session.add(GtmTouch(id=f"sent_activity_{index}", contact_id="contact_activity", venture_id=context["venture_id"],
                workspace_id=context["workspace_id"], direction="out", channel="email", subject=audience, sent_at=now,
                simulated=0, send_mode="founder_gmail", logged_by="runner", created_at=now,
                honesty_json=json.dumps({"channel_experiment_id": identifier}) if index < 2 else "malformed"))
        session.commit()
    outreach_tests.record_reply_activity(context["venture_id"], ["sent_activity_0"], classification="automated", observed_at=now)
    outreach_tests.record_reply_activity(context["venture_id"], ["sent_activity_0", "sent_activity_1"], classification="human_neutral", observed_at=now)
    outreach_tests.record_reply_activity("foreign_venture", ["sent_activity_0"], classification="human_neutral", observed_at=now)
    outreach_tests.record_reply_activity(context["venture_id"], ["sent_activity_2"], classification="human_neutral", observed_at=now)
    assert not activity.list_activity(context["venture_id"])
    outreach_tests.record_reply_activity(context["venture_id"], ["sent_activity_0"], classification="human_positive", observed_at=now)
    item, = activity.list_activity(context["venture_id"])
    assert item["text"] == "1 replied" and item["evidence_level"] == "market_response"
    assert item["option_id"] == "option_0"
    assert datetime.fromisoformat(item["started_at"]) == datetime.fromisoformat(item["ended_at"]) == now

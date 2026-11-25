"""Flag-gated learning, retirement and source-backed corrections across founders."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.memory.test_facts import facts as facts


@pytest.fixture
def founders(facts, monkeypatch):
    from app.chat.conversations import bind_conversation
    from app.db.models import Job, Person, Venture, VentureDocument, Workspace
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.memory import extraction, current
    from app.credits import policy
    from app.tenancy import bind
    import app.state

    flags = {"memory_capture_v1": True, "memory_conflicts_v1": True}
    monkeypatch.setattr(policy, "get_policy", lambda key, default=None: flags if key == "feature_flags" else default)
    monkeypatch.setattr(app.state, "db", lambda: facts.factory)
    facts.Base.metadata.create_all(facts.engine, tables=[Job.__table__, VentureDocument.__table__])
    domains = [SimpleNamespace(workspace="wsp_owned", venture="ven_owned", actor="usr_alice", chat="cnv_owned",
                              audience="logistics teams", other="accounting firms"),
               SimpleNamespace(workspace="wsp_family", venture="ven_family", actor="usr_bob", chat="cnv_family",
                              audience="new parents", other="amateur gardeners")]
    with facts.factory() as session:
        second = domains[1]
        session.add(Person(id=second.actor, display_name="Family owner", created_at=facts.when))
        session.flush()
        session.add(Workspace(id=second.workspace, owner_id=second.actor, kind="venture", name="Family support",
            entry_mode="idea", created_at=facts.when, updated_at=facts.when))
        session.flush()
        session.add(Venture(id=second.venture, workspace_id=second.workspace, name="Family support", raw_idea="Family support",
            stage="draft", created_at=facts.when, updated_at=facts.when))
        session.flush()
        session.add(facts.Conversation(id=second.chat, workspace_id=second.workspace, venture_id=second.venture,
            creator_person_id=second.actor, visibility="private", title="Private", creation_key="private-family",
            created_at=facts.when, updated_at=facts.when))
        session.commit()

    @contextmanager
    def bound(domain):
        app.state.set_factory(facts.factory)
        with bind(workspace_id=domain.workspace, venture_id=domain.venture, actor=domain.actor), bind_conversation(domain.venture, domain.chat):
            yield

    def message(domain, suffix, body, day=1, **changes):
        identity = "msg_" + domain.actor + "_" + suffix
        facts.message(identity, body, day=day, workspace_id=domain.workspace, venture_id=domain.venture,
            conversation_id=domain.chat, author_person_id=domain.actor, **changes)
        return identity

    def website(domain):
        with facts.factory() as session:
            session.add(IdeaSession(id="ids_" + domain.actor, workspace_id=domain.workspace, venture_id=domain.venture,
                case="know", state="confirmed", current_revision_id="idv_" + domain.actor,
                created_at=facts.when, updated_at=facts.when))
            session.flush()
            session.add(IdeaRevision(id="idv_" + domain.actor, session_id="ids_" + domain.actor,
                workspace_id=domain.workspace, venture_id=domain.venture, rev=1, source="website",
                structured=json.dumps({"_website_answers": {"customer": {"answer": domain.other}}}), created_at=facts.when))
            session.commit()

    return SimpleNamespace(d=facts, domains=domains, bound=bound, message=message, website=website,
        extraction=extraction, current=current, flags=flags)


def test_capture_add_update_ignore_and_retire_keep_sources_isolated(founders):
    suite = founders
    for domain in suite.domains:
        with suite.bound(domain):
            first = suite.message(domain, "first", f"Our audience is {domain.audience}.")
            suite.extraction.capture_chat_message(first)
            duplicate = suite.message(domain, "duplicate", f"We target {domain.audience}.", day=2)
            ignored = suite.extraction.capture_chat_message(duplicate)
            assert suite.extraction.capture_chat_message(duplicate) == ignored
            with suite.d.factory() as session:
                assert [row.value for row in suite.d.api.query_facts(session, venture_id=domain.venture)] == [f"Our audience is {domain.audience}."]
                selection = suite.current.select_current_reports(session, venture_id=domain.venture, legacy_hits=[])
                assert [row.value for row in selection["facts"]] == [f"Our audience is {domain.audience}."]
            update = suite.message(domain, "update", f"Our audience is {domain.other}.", day=3, kind="voice_transcript")
            suite.extraction.capture_chat_message(update)
            retirement = suite.message(domain, "retire", f"We no longer target {domain.other}.", day=4)
            retired = suite.extraction.capture_chat_message(retirement)
            assert suite.extraction.capture_chat_message(retirement) == retired
            with suite.d.factory() as session:
                assert suite.d.api.query_facts(session, venture_id=domain.venture) == []
                selection = suite.current.select_current_reports(session, venture_id=domain.venture, legacy_hits=[])
                assert selection["blocked_count"] == 1
                rows = session.scalars(select(suite.d.Fact).where(suite.d.Fact.venture_id == domain.venture)).all()
                assert len(rows) == 4 and all(row.person_id == domain.actor for row in rows)
                assert all(json.loads(row.source_json)["quote"] == row.value for row in rows)


def test_equivalent_subjects_and_predicates_supersede_without_changing_other_founder(founders):
    suite = founders
    for domain in suite.domains:
        with suite.bound(domain), suite.d.factory() as session:
            source = suite.message(domain, "alias", f"Our audience is {domain.audience}.")
            first = suite.d.api.remember_fact(session, venture_id=domain.venture, subject="idea.target_users", predicate="is",
                value=f"Our audience is {domain.audience}.", source={"type": "chat", "ref": "chat:" + source, "quote": f"Our audience is {domain.audience}."})
            session.commit()
            assert first.subject == "idea.audience" and first.predicate == "statement"
        with suite.bound(domain):
            suite.extraction.capture_chat_message(suite.message(domain, "canonical", f"Our audience is {domain.other}.", day=2))
            with suite.d.factory() as session:
                assert [row.value for row in suite.d.api.query_facts(session, venture_id=domain.venture, subject="idea.icp")] == [f"Our audience is {domain.other}."]


def test_quoted_web_mail_documents_and_assistant_messages_cannot_become_founder_facts(founders):
    suite = founders
    for domain in suite.domains:
        with suite.bound(domain):
            for index, body in enumerate((f"> Our audience is {domain.other}.",
                f"Website:\nOur audience is {domain.other}.", f"Email:\nWe no longer target {domain.audience}.",
                f"Document:\nOur audience is {domain.other}.", f"```\nOur audience is {domain.other}.\n```",
                f"Email:\n\nOur audience is {domain.other}.", f"Document:\n\nOur audience is {domain.other}.",
                f"Website:\n\nOur audience is {domain.other}.")):
                identity = suite.message(domain, f"untrusted_{index}", body)
                assert suite.extraction.capture_chat_message(identity) == []
                with suite.d.factory() as session, pytest.raises(ValueError, match="untrusted_quote"):
                    quote = next(line.removeprefix("> ") for line in body.splitlines()
                                 if "Our audience" in line or "We no longer" in line)
                    suite.d.api.remember_fact(session, venture_id=domain.venture, subject="idea.audience", predicate="statement",
                        value=quote, source={"type": "chat", "ref": "chat:" + identity, "quote": quote})
            assert suite.extraction.capture_chat_message(suite.message(domain, "assistant", f"Our audience is {domain.other}.", role="assistant")) == []


@pytest.mark.parametrize("choice", [0, 1, 2])
def test_sourced_conflict_card_choice_is_a_durable_correction_and_never_reasked(founders, choice):
    suite = founders
    for domain in suite.domains:
        suite.website(domain)
        with suite.bound(domain):
            suite.extraction.capture_chat_message(suite.message(domain, "stated", f"Our audience is {domain.audience}."))
            with suite.d.factory() as session:
                cards = suite.current.conflict_cards(session, venture_id=domain.venture)
                assert len(cards) == 1
                card = cards[0]
                assert [item["value"] for item in card["choices"]] == [domain.other, f"Our audience is {domain.audience}."]
                assert all(item["ref"] and item["timestamp"] for item in card["choices"])
                assert "October 02, 2026" in card["question"] and "you told me" in card["question"]
                saved_id = suite.message(domain, "card", card["question"], role="assistant", kind="memory_conflict")
                session.get(suite.d.Message, saved_id).actions_json = json.dumps([{"confirmation": {"action": "chat.reply",
                    "resulting_state": {"memory_conflict": card}}}])
                session.commit()
            value = card["choices"][choice]["value"] if choice < 2 else "Both are true:\n" + "\n".join(item["value"] for item in card["choices"])
            correction = suite.message(domain, "choice", f"Memory correction {saved_id}: {choice}\n{value}", day=2)
            ids = suite.extraction.capture_chat_message(correction)
            assert len(ids) == 1 and suite.extraction.capture_chat_message(correction) == ids
            with suite.d.factory() as session:
                assert suite.current.conflict_cards(session, venture_id=domain.venture) == []
                current = suite.d.api.query_facts(session, venture_id=domain.venture)
                assert [row.value for row in current] == [value]
                assert json.loads(current[0].source_json)["ref"] == "chat:" + correction


def test_flag_off_preserves_original_capture_and_selection_payload(founders):
    suite = founders
    suite.flags.clear()
    for domain in suite.domains:
        with suite.bound(domain):
            identity = suite.message(domain, "off", f"Our audience is {domain.audience}.")
            assert suite.extraction.schedule_capture(identity) is None
            assert len(suite.extraction.capture_chat_message(identity)) == 1
            with suite.d.factory() as session:
                selection = suite.current.select_current_reports(session, venture_id=domain.venture, legacy_hits=[])
                assert "conflict_cards" not in selection
                assert selection["conflicts"] == []
                assert selection["facts"][0].value == f"Our audience is {domain.audience}."


def test_durable_store_hook_queues_chat_and_voice_once_never_extracts_on_reply_path(founders, monkeypatch):
    from app.chat.store import append_message
    from app.core import jobs
    from app.tenancy import bind
    suite = founders
    queued = []

    def enqueue(session, kind, payload, **scope):
        with suite.d.factory() as durable:
            row = durable.get(suite.d.Message, payload["source_message_id"])
            assert row is not None and row.author_person_id == domain.actor
        assert kind == "memory.extract" and scope["workspace_id"] == domain.workspace
        queued.append(payload)
        return "job_saved"

    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(suite.extraction, "capture_chat_message", lambda _: pytest.fail("capture ran on reply path"))
    for domain in suite.domains:
        with suite.bound(domain):
            for kind in ("text", "voice_transcript", "memory_command"):
                first = append_message(domain.venture, role="user", body_md=f"Our audience is {domain.audience}.",
                    kind=kind, idempotency_key=kind)
                again = append_message(domain.venture, role="user", body_md=f"Our audience is {domain.audience}.",
                    kind=kind, idempotency_key=kind)
                assert first == again and queued[-1]["source_message_id"] == first["id"]
        with bind(workspace_id=domain.workspace, venture_id=domain.venture, actor=domain.actor):
            append_message(domain.venture, role="user", body_md=f"Our audience is {domain.audience}.",
                conversation_id=domain.chat, idempotency_key="without_bound_context")
    assert len(queued) == 8


def test_foreign_sources_cannot_schedule_or_resolve_another_founders_memory(founders):
    suite = founders
    first, second = suite.domains
    identity = suite.message(first, "private", f"Our audience is {first.audience}.")
    with suite.bound(second):
        assert suite.extraction.capture_chat_message(identity) == []
        assert suite.extraction.schedule_capture(identity) is None


def test_shared_surface_can_capture_your_private_turn_but_never_a_shared_source(founders, monkeypatch):
    from app.chat.conversations import bind_conversation, current_conversation
    from app.chat.store import append_message
    from app.core import jobs
    from app.tenancy import bind
    from app.state import set_factory

    suite = founders
    set_factory(suite.d.factory)
    queued = []
    monkeypatch.setattr(jobs, "enqueue", lambda session, kind, payload, **scope:
        queued.append((payload, current_conversation())) or "job_private")
    for domain in suite.domains:
        with bind(workspace_id=domain.workspace, venture_id=domain.venture, actor=domain.actor), bind_conversation(domain.venture):
            shared = current_conversation()
            assert shared.visibility == "legacy_shared"
            excluded = "msg_" + domain.actor + "_shared_source"
            suite.d.message(excluded, f"Our audience is {domain.audience}.", workspace_id=domain.workspace,
                venture_id=domain.venture, conversation_id=shared.id, author_person_id=domain.actor)
            assert suite.extraction.schedule_capture(excluded) is None
            assert suite.extraction.capture_chat_message(excluded) == []
            for kind in ("text", "voice_transcript"):
                saved = append_message(domain.venture, conversation_id=domain.chat, role="user", kind=kind,
                    body_md=f"Our audience is {domain.audience}.", idempotency_key="surface_" + kind)
                payload, admitted = queued[-1]
                assert payload["source_message_id"] == saved["id"] and admitted.id == domain.chat
                assert admitted.visibility == "private" and admitted.actor_id == domain.actor
    assert len(queued) == 4


def test_corrections_retire_all_pre_flag_subject_aliases_without_resurrecting_old_values(founders):
    suite = founders
    for domain in suite.domains:
        with suite.bound(domain):
            suite.flags.clear()
            for day, alias in enumerate(("idea.target_users", "idea.customers"), 1):
                body = f"Our audience is {domain.audience}."
                identity = suite.message(domain, f"preflag_{day}", body, day=day)
                with suite.d.factory() as session:
                    suite.d.api.remember_fact(session, venture_id=domain.venture, subject=alias, predicate="is", value=body,
                        source={"type": "chat", "ref": "chat:" + identity, "quote": body})
                    session.commit()
            suite.flags.update(memory_capture_v1=True, memory_conflicts_v1=True)
            suite.extraction.capture_chat_message(suite.message(domain, "postflag", f"Our audience is {domain.other}.", day=3))
            with suite.d.factory() as session:
                assert [row.value for row in suite.d.api.query_facts(session, venture_id=domain.venture)] == [f"Our audience is {domain.other}."]
            suite.extraction.capture_chat_message(suite.message(domain, "postflag_retire", f"We no longer target {domain.other}.", day=4))
            with suite.d.factory() as session:
                assert suite.d.api.query_facts(session, venture_id=domain.venture) == []
                assert suite.current.select_current_reports(session, venture_id=domain.venture, legacy_hits=[])["facts"] == []

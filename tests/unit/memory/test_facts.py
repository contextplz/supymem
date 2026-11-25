"""Offline source custody, corrections, retained history, and private actors."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture
def facts(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_FACTS_ENABLED", "1")
    with service_modules("company_os"):
        from app.db.models import Base, ChatConversation, ChatMessage, Person, Venture, Workspace
        from app.db.fact_models import VentureFact
        from app.db.session import make_engine, make_session_factory
        from app.memory import facts as api
        from app.tenancy import bind
        engine = make_engine(f"sqlite:///{tmp_path}/facts.db")
        # Only these source tables are needed; no application boot or providers.
        Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in
            ("people", "workspaces", "ventures", "chat_conversations", "chat_messages", "venture_facts",
             "idea_sessions", "idea_revisions", "clarifier_turns")])
        factory = make_session_factory(engine)
        when = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with factory() as session:
            session.add(Person(id="usr_alice", display_name="Owner", created_at=when))
            session.flush()
            session.add(Workspace(id="wsp_owned", kind="venture", name="Test", entry_mode="idea",
                owner_id="usr_alice", created_at=when, updated_at=when))
            session.flush()
            session.add(Venture(id="ven_owned", workspace_id="wsp_owned", name="Test", raw_idea="Test",
                stage="draft", created_at=when, updated_at=when))
            session.flush()
            session.add(ChatConversation(id="cnv_owned", workspace_id="wsp_owned", venture_id="ven_owned",
                creator_person_id="usr_alice", visibility="private", title="Private", creation_key="private",
                created_at=when, updated_at=when))
            session.commit()

        def message(identity, body, *, day=0, **changes):
            values = dict(id=identity, workspace_id="wsp_owned", venture_id="ven_owned",
                conversation_id="cnv_owned", author_person_id="usr_alice", role="user", kind="text",
                body_md=body, created_at=when + timedelta(days=day))
            with factory() as session:
                session.add(ChatMessage(**(values | changes)))
                session.commit()
            return {"type": "chat", "ref": "chat:" + identity, "quote": body}

        def write(source, **changes):
            with factory() as session:
                row = api.remember_fact(session, venture_id="ven_owned", subject="idea.audience",
                    predicate="is", value=source["quote"], source=source, **changes)
                identity = row.id
                session.commit()
                return identity

        def read(**changes):
            with factory() as session:
                return [api.project_fact(row) for row in api.query_facts(session, venture_id="ven_owned", **changes)]

        @contextmanager
        def private(*, conversation_id="cnv_owned", actor="usr_alice"):
            from app.chat.conversations import bind_conversation
            from app.state import set_factory
            set_factory(factory)
            with bind(workspace_id="wsp_owned", venture_id="ven_owned", actor=actor), bind_conversation("ven_owned", conversation_id):
                yield

        yield SimpleNamespace(api=api, factory=factory, message=message, write=write, read=read,
            bind=lambda **kw: bind(workspace_id="wsp_owned", venture_id="ven_owned", actor=kw.get("actor", "usr_alice")),
            bind_private=private,
            Fact=VentureFact, Message=ChatMessage, Conversation=ChatConversation, when=when,
            Base=Base, engine=engine)
        engine.dispose()


@pytest.mark.parametrize("audience", ["Warehouse purchasing teams", "Families planning meals"])
def test_source_schema_can_be_created_again_without_losing_facts(facts, audience):
    source = facts.message("msg_schema", audience)
    with facts.bind():
        identity = facts.write(source)
        facts.Base.metadata.create_all(facts.engine)
        facts.Base.metadata.create_all(facts.engine)
        assert facts.read()[0]["id"] == identity
        assert facts.read()[0]["value"] == audience


def test_latest_wins_preserves_both_times_history_and_replay(facts):
    d = facts
    a = d.message("msg_a", "Our audience is teachers.")
    b = d.message("msg_b", "Our audience is nurses.", day=1)
    c = d.message("msg_c", "Our audience is dentists.", day=2)
    with d.bind():
        first = d.write(a)
        second = d.write(b)
        third = d.write(c)
        assert d.write(a) == first
        assert d.write(b) == second
        assert [row["id"] for row in d.read()] == [third]
        history = d.read(include_history=True)
        assert {row["id"] for row in history} == {first, second, third}
        assert all(row["source"]["quote"] == row["value"] and row["effect_authority"] is False for row in history)
        assert all(row["superseded_at"] and row["valid_to"] for row in history if row["id"] != third)
        assert [row["value"] for row in d.read(as_of=d.when + timedelta(hours=12))] == [a["quote"]]


def test_delayed_older_statement_cannot_revive_over_current_correction(facts):
    d = facts
    old = d.message("msg_old", "Audience: teachers.")
    current = d.message("msg_current", "Audience: nurses.", day=2)
    with d.bind():
        newer = d.write(current)
        older = d.write(old)
        assert d.write(old) == older
        assert [row["id"] for row in d.read()] == [newer]
        assert len(d.read(include_history=True)) == 2
        assert d.read(as_of=d.when + timedelta(days=1))[0]["id"] == older


def test_event_time_history_returns_latest_known_value_after_out_of_order_capture(facts):
    d = facts
    a = d.message("msg_old", "Audience: teachers.")
    b = d.message("msg_middle", "Audience: nurses.", day=1)
    c = d.message("msg_current", "Audience: dentists.", day=2)
    with d.bind():
        d.write(c)
        d.write(a)
        middle = d.write(b)
        assert [row["id"] for row in d.read(as_of=d.when + timedelta(days=1, hours=1))] == [middle]


def test_late_middle_source_does_not_overlap_earlier_source_or_revive_after_withdrawal(facts):
    d = facts
    a = d.message("msg_original", "Audience: teachers.")
    b = d.message("msg_middle", "Audience: nurses.", day=1)
    c = d.message("msg_current", "Audience: dentists.", day=2)
    with d.bind():
        d.write(a)
        newest = d.write(c)
        middle = d.write(b)
        at = d.when + timedelta(days=1, hours=1)
        assert [row["id"] for row in d.read(as_of=at)] == [middle]
        assert [row["id"] for row in d.read()] == [newest]
        with d.factory() as session:
            session.get(d.Message, "msg_middle").body_md = "Changed or withdrawn source"
            session.commit()
        assert d.read(as_of=at) == []


def test_event_and_system_time_can_reconstruct_before_and_after_learning_a_correction(facts, monkeypatch):
    d = facts
    original = d.message("msg_original", "Audience: teachers.")
    correction = d.message("msg_correction", "Audience: nurses.", day=1)
    learned_a, learned_b = d.when + timedelta(days=10), d.when + timedelta(days=11)
    with d.bind():
        monkeypatch.setattr(d.api, "utcnow", lambda: learned_a)
        first = d.write(original)
        monkeypatch.setattr(d.api, "utcnow", lambda: learned_b)
        second = d.write(correction)
        before_learning = learned_a + timedelta(hours=1)
        event_after_change = d.when + timedelta(days=2)
        assert [row["id"] for row in d.read(recorded_as_of=before_learning)] == [first]
        assert [row["id"] for row in d.read(as_of=event_after_change, recorded_as_of=before_learning)] == [first]
        assert [row["id"] for row in d.read(as_of=event_after_change, recorded_as_of=learned_b)] == [second]
        assert [row["id"] for row in d.read(as_of=d.when + timedelta(hours=1), recorded_as_of=learned_b)] == [first]
        assert len(d.read(include_history=True, recorded_as_of=learned_b)) == 2


@pytest.mark.parametrize("changes", [
    {"role": "assistant"}, {"role": "system"}, {"author_person_id": "usr_bob"},
    {"workspace_id": "wsp_foreign"}, {"venture_id": "ven_foreign"},
    {"kind": "voice_summary"},
])
def test_assistant_and_unowned_sources_are_refused(facts, changes):
    d = facts
    source = d.message("msg_bad", "Audience: nurses.", **changes)
    with d.bind(), pytest.raises(ValueError, match="memory_fact_founder_source_invalid"):
        d.write(source)


def test_private_conversation_owner_is_required_even_for_same_author(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.factory() as session:
        session.get(d.Conversation, "cnv_owned").creator_person_id = "usr_bob"
        session.commit()
    with d.bind(), pytest.raises(ValueError, match="memory_fact_founder_source_invalid"):
        d.write(source)


def test_source_tampering_and_different_actor_hide_fact(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.bind():
        identity = d.write(source)
        assert d.read()[0]["id"] == identity
    with d.bind(actor="usr_bob"):
        assert d.read() == []
        with d.factory() as session, pytest.raises(KeyError):
            d.api.forget_fact(session, venture_id="ven_owned", fact_id=identity)
    with d.factory() as session:
        session.get(d.Message, "msg_owned").body_md = "Audience: teachers."
        session.commit()
    with d.bind():
        assert d.read() == [] and d.read(include_history=True) == []


def test_forget_retains_row_and_source_but_replay_stays_forgotten(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.bind():
        identity = d.write(source)
        with d.factory() as session:
            d.api.forget_fact(session, venture_id="ven_owned", fact_id=identity)
            session.commit()
        assert d.read() == []
        assert d.write(source) == identity
        assert d.read(include_history=True) == []
        with d.factory() as session:
            row = session.get(d.Fact, identity)
            assert row.forgotten_at and row.source_json and row.value == source["quote"]


def test_delayed_older_source_after_forget_does_not_restore_an_audience(facts):
    d = facts
    earlier = d.message("msg_earlier", "Audience: teachers.")
    latest = d.message("msg_latest", "Audience: nurses.", day=1)
    with d.bind():
        identity = d.write(latest)
        with d.factory() as session:
            d.api.forget_fact(session, venture_id="ven_owned", fact_id=identity)
            session.commit()
        d.write(earlier)
        assert d.read() == []


def test_only_literal_source_quote_can_be_written_and_retries_cannot_change_selection(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses. Budget: fixed.")
    with d.bind():
        selected = source | {"quote": "Audience: nurses."}
        d.write(selected)
        with pytest.raises(ValueError, match="memory_fact_idempotency_conflict"):
            d.write(source | {"quote": "Budget: fixed."})
        with pytest.raises(ValueError, match="memory_fact_quote_unproven"):
            d.write(source | {"quote": "Audience: doctors."})


@pytest.mark.parametrize("field,value", [("subject", "idea audience"), ("predicate", "is;drop"),
    ("confidence", float("nan")), ("confidence", True), ("confidence", 1.01)])
def test_invalid_keys_and_confidence_fail_closed(facts, field, value):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.bind(), d.factory() as session, pytest.raises(ValueError):
        kwargs = dict(venture_id="ven_owned", subject="idea.audience", predicate="is",
                      value=source["quote"], source=source)
        d.api.remember_fact(session, **(kwargs | {field: value}))


def test_normalized_keys_and_secret_guard(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.bind(), d.factory() as session:
        row = d.api.remember_fact(session, venture_id="ven_owned", subject=" Idea.Audience ", predicate=" IS ",
                                  value=source["quote"], source=source)
        assert row.subject == "idea.audience" and row.predicate == "is"
        session.commit()
    secret = "api" + "_key=" + "private-test-value-" * 4
    source = d.message("msg_secret", secret)
    with d.bind(), pytest.raises(ValueError, match="memory_credential_withheld"):
        d.write(source)


def test_source_time_cannot_be_model_authored_and_system_scope_cannot_read(facts):
    d = facts
    source = d.message("msg_owned", "Audience: nurses.")
    with d.bind(), pytest.raises(ValueError, match="memory_fact_event_time_unproven"):
        d.write(source, valid_from=d.when + timedelta(days=1))
    with d.bind(actor="system"), pytest.raises(PermissionError, match="memory_fact_actor_required"):
        d.read()


@pytest.fixture
def research_fact(facts):
    import json
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding

    d = facts
    d.Base.metadata.create_all(d.engine, tables=[d.Base.metadata.tables[name] for name in
        ("idea_sessions", "research_dossiers", "research_findings")])
    with d.factory() as session:
        session.add(IdeaSession(id="ids_owned", workspace_id="wsp_owned", venture_id="ven_owned",
            case="know", state="confirmed", current_revision_id="idv_owned", confirmed_revision_id="idv_owned",
            created_at=d.when, updated_at=d.when))
        session.flush()
        session.add(IdeaRevision(id="idv_owned", session_id="ids_owned", workspace_id="wsp_owned", venture_id="ven_owned",
            rev=1, source="founder", structured='{"customer":"scheduling users"}', created_at=d.when))
        session.add(ResearchDossier(id="rsd_owned", workspace_id="wsp_owned", venture_id="ven_owned",
            revision_id="idv_owned", kind="deep", state="complete", summary="Generated assessment is not evidence.",
            created_at=d.when, completed_at=d.when))
        session.flush()
        session.add(ResearchFinding(id="rsf_owned", dossier_id="rsd_owned", workspace_id="wsp_owned",
            venture_id="ven_owned", category="source", title="Example source", url="https://example.test/offer",
            source="exa", summary="The provider excerpt mentions nurses.",
            evidence=json.dumps({"evidence_kind": "search_excerpt", "observed_at": d.when.isoformat()}), created_at=d.when))
        session.commit()
    return SimpleNamespace(d=d, Dossier=ResearchDossier, Finding=ResearchFinding, Idea=IdeaSession, Revision=IdeaRevision,
        source={"type": "research", "ref": "research:rsf_owned", "quote": "The provider excerpt mentions nurses."})


def test_research_is_observation_only_and_never_supersedes_founder_subject(research_fact):
    r, d = research_fact, research_fact.d
    with d.bind(), d.factory() as session:
        assert d.api.source_options(session, venture_id="ven_owned", source_type="research",
                                    reference=r.source["ref"]) == [r.source["quote"]]
        with pytest.raises(ValueError, match="memory_fact_source_authority_invalid"):
            d.api.remember_fact(session, venture_id="ven_owned", subject="idea.audience", predicate="is",
                                value=r.source["quote"], source=r.source)
        row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.audience", predicate="mentions",
                                value=r.source["quote"], source=r.source)
        assert d.api.project_fact(row)["qualification"] == "source_observation_context_only"
        session.commit()
    with d.bind(), d.factory() as session, pytest.raises(ValueError, match="memory_fact_quote_unproven"):
        source = r.source | {"quote": "Generated assessment is not evidence."}
        d.api.remember_fact(session, venture_id="ven_owned", subject="research.audience", predicate="mentions",
                            value=source["quote"], source=source)


@pytest.mark.parametrize("tamper", ["excluded", "outdated", "pending", "model", "url", "quote", "revision"])
def test_research_revalidates_current_idea_exclusion_provider_and_exact_source(research_fact, tamper):
    import json
    r, d = research_fact, research_fact.d
    with d.bind(), d.factory() as session:
        row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.audience", predicate="mentions",
                                value=r.source["quote"], source=r.source)
        session.commit()
        assert d.api.source_current(session, row)
    with d.factory() as session:
        finding, dossier = session.get(r.Finding, "rsf_owned"), session.get(r.Dossier, "rsd_owned")
        if tamper == "excluded":
            finding.evidence = json.dumps({"evidence_kind": "search_excerpt", "founder_flag": "exclude"})
        elif tamper == "outdated":
            session.add(r.Revision(id="idv_newer", session_id="ids_owned", workspace_id="wsp_owned", venture_id="ven_owned",
                rev=2, source="founder", structured='{"customer":"newer audience"}', created_at=d.when + timedelta(days=1)))
            session.get(r.Idea, "ids_owned").current_revision_id = "idv_newer"
        elif tamper == "pending":
            dossier.state = "running"
        elif tamper == "model":
            finding.source = "model"
        elif tamper == "url":
            finding.url = "https://foreign.test/claim"
        elif tamper == "revision":
            dossier.revision_id = "idv_different"
        else:
            finding.summary = "The provider excerpt mentions dentists."
        session.commit()
    with d.bind():
        assert d.read() == []


def prior_art_row(r, kind, provider):
    """Persist the metadata/excerpt shape written by S09's provider adapters."""
    import json
    d = r.d
    prior = {"source_kind": kind, "company_name": "Seed Attempt", "company_url": "https://example.test/",
             "status": "unknown", "date": d.when.isoformat()}
    url, category, repository = "https://example.test/pricing", "source", None
    if kind == "hn_item":
        prior.update(hn_id=101, launch_type="show_hn")
        url = "https://news.ycombinator.com/item?id=101"
    elif kind == "hn_comment":
        prior.update(hn_id=202, story_id=101)
        url = "https://news.ycombinator.com/item?id=202"
    elif kind == "yc_company":
        prior.update(yc_status="Active", cache_stale=False)
        url = "https://www.ycombinator.com/companies/seed-attempt"
    elif kind in {"github_issue", "repository_metadata"}:
        prior["company_url"] = "https://github.com/contextplz/seed-attempt"
        url = prior["company_url"] + ("/issues/3" if kind == "github_issue" else "")
        if kind == "repository_metadata":
            category = "oss_repo"
            repository = {"full_name": "contextplz/seed-attempt", "private": False, "stars": 3}
    evidence = {"observed_at": d.when.isoformat(), "evidence_kind": kind, "prior_art": prior,
                "repository": repository, "page_verified": False, "search_queries": ["booking"], "search_intents": ["prior_art"]}
    with d.factory() as session:
        row = session.get(r.Finding, "rsf_owned")
        row.source, row.category, row.url, row.title = provider, category, url, "Seed Attempt"
        row.summary = "The public provider excerpt describes a scheduling attempt."
        row.evidence = json.dumps(evidence)
        session.commit()
    return evidence, {"type": "research", "ref": "research:rsf_owned", "quote": "The public provider excerpt describes a scheduling attempt."}


@pytest.mark.parametrize("kind,provider", [("hn_item", "hn_algolia"), ("hn_comment", "hn_firebase"),
    ("yc_company", "yc_oss"), ("github_issue", "github"), ("repository_metadata", "github"),
    ("web", "exa"), ("web", "firecrawl"), ("web", "tavily"), ("web", "duckduckgo")])
def test_actual_typed_prior_art_provider_rows_are_source_bound_observations(research_fact, kind, provider):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, kind, provider)
    with d.bind(), d.factory() as session:
        row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                  value=source["quote"], source=source)
        session.commit()
        assert d.api.source_current(session, row)
        assert d.api.project_fact(row)["effect_authority"] is False
        assert d.api.project_fact(row)["qualification"] == "source_observation_context_only"
    with d.factory() as session:
        evidence["prior_art"]["company_name"] = "Changed provenance"
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind():
        assert d.read() == []  # Persisted source provenance is part of the immutable proof.


@pytest.mark.parametrize("private", [False, True])
def test_legacy_public_repository_metadata_without_prior_art_is_retained(research_fact, private):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "repository_metadata", "github")
    evidence.pop("prior_art")
    evidence["repository"]["private"] = private
    with d.factory() as session:
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session:
        if private:
            with pytest.raises(ValueError, match="memory_fact_research_source_invalid"):
                d.api.remember_fact(session, venture_id="ven_owned", subject="research.repository", predicate="reports",
                                    value=source["quote"], source=source)
        else:
            row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.repository", predicate="reports",
                                      value=source["quote"], source=source)
            assert d.api.source_current(session, row)


@pytest.mark.parametrize("tamper", [None, "wrong_provider", "model_field", "unknown_timestamp", "fake_page"])
def test_typed_additional_excerpt_matches_actual_provider_record_shape(research_fact, tamper):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "hn_item", "hn_algolia")
    extra = "The next API excerpt mentions a scheduling workflow."
    entry = {"text": extra, "query": "scheduling", "provider": "hn_algolia", "observed_at": d.when.isoformat()}
    if tamper == "wrong_provider":
        entry["provider"] = "hn_firebase"
    elif tamper == "model_field":
        entry["model_assessment"] = True
    elif tamper == "unknown_timestamp":
        entry["observed_at"] = "unknown"
    elif tamper == "fake_page":
        entry.update(provider="firecrawl", query="page_scrape", verified_page=True)
        evidence["page_verified"] = True
    evidence["additional_excerpts"] = [entry]
    with d.factory() as session:
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session:
        source = source | {"quote": extra}
        if tamper:
            with pytest.raises(ValueError, match="memory_fact_quote_unproven"):
                d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                    value=extra, source=source)
        else:
            row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                      value=extra, source=source)
            assert d.api.source_current(session, row)


@pytest.mark.parametrize("kind,provider,tamper", [
    ("hn_item", "hn_algolia", "wrong_provider"), ("hn_item", "hn_algolia", "wrong_category"),
    ("hn_item", "hn_algolia", "missing_prior"), ("hn_item", "hn_algolia", "wrong_kind"),
    ("hn_item", "hn_algolia", "url"), ("hn_item", "hn_algolia", "bool_id"),
    ("hn_comment", "hn_firebase", "story_id"), ("yc_company", "yc_oss", "url"),
    ("yc_company", "yc_oss", "title"), ("github_issue", "github", "url"),
    ("web", "exa", "company_url"), ("web", "exa", "missing_observed"),
    ("web", "exa", "model_kind"), ("web", "exa", "excluded"), ("web", "exa", "outdated"),
    ("web", "exa", "arbitrary_kind"),
])
def test_forged_or_retired_typed_provider_evidence_is_not_admitted(research_fact, kind, provider, tamper):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, kind, provider)
    with d.factory() as session:
        row = session.get(r.Finding, "rsf_owned")
        if tamper == "wrong_provider":
            row.source = "exa"
        elif tamper == "wrong_category":
            row.category = "oss_repo"
        elif tamper == "missing_prior":
            evidence.pop("prior_art")
        elif tamper == "wrong_kind":
            evidence["prior_art"]["source_kind"] = "web"
        elif tamper == "url":
            row.url = "https://foreign.test/claim"
        elif tamper == "bool_id":
            evidence["prior_art"]["hn_id"] = True
        elif tamper == "story_id":
            evidence["prior_art"].pop("story_id")
        elif tamper == "title":
            row.title = "Unrelated generated title"
        elif tamper == "company_url":
            evidence["prior_art"]["company_url"] = "https://foreign.test/"
        elif tamper == "missing_observed":
            evidence.pop("observed_at")
        elif tamper == "model_kind":
            evidence["evidence_kind"] = "model_assessment"
        elif tamper == "arbitrary_kind":
            evidence["evidence_kind"] = {"model_assessment": "An arbitrary JSON body"}
        elif tamper == "excluded":
            evidence["founder_flag"] = "exclude"
        else:
            session.add(r.Revision(id="idv_newer", session_id="ids_owned", workspace_id="wsp_owned", venture_id="ven_owned",
                rev=2, source="founder", structured='{"customer":"newer audience"}', created_at=d.when + timedelta(days=1)))
            session.get(r.Idea, "ids_owned").current_revision_id = "idv_newer"
        row.evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session, pytest.raises(ValueError, match="memory_fact_research_source_invalid"):
        d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                            value=source["quote"], source=source)


def test_generated_lessons_and_additional_wrong_provider_prose_are_not_quote_sources(research_fact):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "hn_item", "hn_algolia")
    generated = "The model concludes the company failed because nobody paid."
    evidence["prior_art"]["lessons"] = generated
    evidence["additional_excerpts"] = [{"text": generated, "query": "booking", "provider": "model",
                                         "observed_at": d.when.isoformat()}]
    with d.factory() as session:
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session, pytest.raises(ValueError, match="memory_fact_quote_unproven"):
        source = source | {"quote": generated}
        d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                            value=generated, source=source)


@pytest.mark.parametrize("valid,change", [(True, None), (False, "provider"), (False, "query"),
                                         (False, "verified_page"), (False, "page_verified")])
def test_verified_page_quote_requires_actual_firecrawl_source_fields(research_fact, valid, change):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "web", "exa")
    page = "The fetched public page describes their booking workflow."
    entry = {"text": page, "query": "page_scrape", "provider": "firecrawl",
             "verified_page": True, "observed_at": d.when.isoformat()}
    evidence["page_verified"] = True
    if change == "provider":
        entry["provider"] = "model"
    elif change == "query":
        entry["query"] = "model_review"
    elif change == "verified_page":
        entry["verified_page"] = False
    elif change == "page_verified":
        evidence["page_verified"] = False
    evidence["additional_excerpts"] = [entry]
    with d.factory() as session:
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session:
        source = source | {"quote": page}
        if valid:
            fact = d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                       value=page, source=source)
            assert d.api.source_current(session, fact)
        else:
            with pytest.raises(ValueError, match="memory_fact_quote_unproven"):
                d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                    value=page, source=source)


def test_public_research_quotes_do_not_become_contact_memory(research_fact):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "hn_comment", "hn_firebase")
    contact_quote = "Contact " + "founder" + "@" + "real-mail-domain.dev" + " to buy our service."
    with d.factory() as session:
        row = session.get(r.Finding, "rsf_owned")
        row.summary, row.evidence = contact_quote, json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session, pytest.raises(ValueError, match="memory_external_contact_withheld"):
        d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                            value=contact_quote, source=source | {"quote": contact_quote})


@pytest.mark.parametrize("company,accepted", [("https://www.example.test/", True),
    ("https://EXAMPLE.test/", True), ("https://foreign.test/", False), ("http://example.test/", False)])
def test_web_provider_canonical_www_host_alias_keeps_scheme_custody(research_fact, company, accepted):
    import json
    r, d = research_fact, research_fact.d
    evidence, source = prior_art_row(r, "web", "exa")
    evidence["prior_art"]["company_url"] = company
    with d.factory() as session:
        session.get(r.Finding, "rsf_owned").evidence = json.dumps(evidence)
        session.commit()
    with d.bind(), d.factory() as session:
        if accepted:
            row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                      value=source["quote"], source=source)
            assert d.api.source_current(session, row)
        else:
            with pytest.raises(ValueError, match="memory_fact_research_source_invalid"):
                d.api.remember_fact(session, venture_id="ven_owned", subject="research.prior_attempt", predicate="reports",
                                    value=source["quote"], source=source)


def test_research_currentness_follows_updated_current_revision_over_created_session_order(research_fact):
    r, d = research_fact, research_fact.d
    with d.factory() as session:
        session.get(r.Idea, "ids_owned").created_at = d.when + timedelta(days=1)
        session.add(r.Idea(id="ids_older_created", workspace_id="wsp_owned", venture_id="ven_owned",
            case="know", state="confirmed", current_revision_id="idv_updated_current", confirmed_revision_id="idv_updated_current",
            created_at=d.when, updated_at=d.when + timedelta(days=2)))
        session.flush()
        session.add(r.Revision(id="idv_updated_current", session_id="ids_older_created", workspace_id="wsp_owned",
            venture_id="ven_owned", rev=1, source="founder", structured='{"customer":"current audience"}', created_at=d.when))
        session.commit()
    with d.bind(), d.factory() as session, pytest.raises(ValueError, match="memory_fact_research_source_invalid"):
        d.api.remember_fact(session, venture_id="ven_owned", subject="research.audience", predicate="reports",
                            value=r.source["quote"], source=r.source)
    with d.factory() as session:
        session.get(r.Dossier, "rsd_owned").revision_id = "idv_updated_current"
        session.commit()
    with d.bind(), d.factory() as session:
        row = d.api.remember_fact(session, venture_id="ven_owned", subject="research.audience", predicate="reports",
                                  value=r.source["quote"], source=r.source)
        assert d.api.source_current(session, row)


@pytest.mark.parametrize("kind", ["import", "task"])
def test_artifacts_admit_only_exact_durable_receipt_not_model_report(facts, kind):
    import json
    from app.db.import_models import ImportRun, SourceSnapshot
    from app.db.models import Job, PlannedTask, Venture

    d = facts
    run = SimpleNamespace(workspace_id="wsp_owned", venture_id="ven_owned", actor_subject="usr_alice",
                          source_snapshot_id="src_owned", state="report_complete")
    snapshot = SimpleNamespace(id="src_owned", import_run_id="imp_owned", workspace_id="wsp_owned",
        venture_id="ven_owned", source_provenance_json='{"storage_verified":true}',
        normalized_archive_sha256="a" * 64, manifest_digest="b" * 64, size_bytes=100, created_at=d.when)
    task = SimpleNamespace(id="tsk_owned", workspace_id="wsp_owned", venture_id="ven_owned", status="done", finished_at=d.when)
    job = SimpleNamespace(id="job_owned", workspace_id="wsp_owned", venture_id="ven_owned", kind="task.run",
        status="succeeded", finished_at=d.when, payload_json=json.dumps({"task_id": task.id, "_actor_subject": "usr_alice"}))
    rows = {(Venture, "ven_owned"): SimpleNamespace(id="ven_owned", workspace_id="wsp_owned"),
            (ImportRun, "imp_owned"): run, (SourceSnapshot, snapshot.id): snapshot,
            (PlannedTask, task.id): task, (Job, job.id): job}
    session = SimpleNamespace(get=lambda model, identity: rows.get((model, identity)))
    ref = "import:src_owned" if kind == "import" else "task:job_owned"
    with d.bind():
        quotes = d.api.source_options(session, venture_id="ven_owned", source_type=kind, reference=ref)
        assert len(quotes) == 1 and json.loads(quotes[0])
        proof, when, qualification = d.api._proof(session, venture_id="ven_owned",
            source={"type": kind, "ref": ref, "quote": quotes[0]})
        assert len(proof) == 64 and when == d.when.replace(tzinfo=None) and qualification.endswith("only")
        for unproven in ("Generated report says product launch succeeded.", quotes[0][:20]):
            with pytest.raises(ValueError, match="memory_fact_quote_unproven"):
                d.api._proof(session, venture_id="ven_owned", source={"type": kind, "ref": ref, "quote": unproven})
        if kind == "import":
            run.actor_subject = "usr_other"
        else:
            task.status = "open"
        with pytest.raises(ValueError, match="memory_fact_.*_source_invalid"):
            d.api.source_options(session, venture_id="ven_owned", source_type=kind, reference=ref)

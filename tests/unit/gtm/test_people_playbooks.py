import asyncio
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import append_idea_revision, load_company_os_app
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as postgres_journey_db
from tests.unit.gtm.test_journey import _add, _mail, journey_db  # noqa: F401


def test_generated_email_truth_corpus_and_founder_provenance(request, monkeypatch):
    from app.autopilot import claims, outreach
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import validate_send

    honest = [
        "How do you schedule kiln reservations today?",
        "What does your equipment inspection process look like?",
        "Could you share how you assign repair jobs?",
        "Have you tried a shared calendar for studio bookings?",
        "What gets in your way when planning a firing?",
        "I'm building a reservation planner for pottery studios.",
        "We are working on a tool for maintenance coordinators.",
        "I would like to understand how your team handles cancellations.",
        "How do you find a walking companion today?",
        "What helps you plan family outings?",
        "Could you describe how you choose a picnic location?",
        "Have you used an app to organize neighborhood walks?",
        "I'm building an outing planner for families.",
        "We are building a way for hobby gardeners to swap seeds.",
        "Could we talk about how you plan an outing?",
    ]
    invented = [
        "I keep hearing from pottery studio owners that reservations take hours.",
        "We've spoken with maintenance coordinators about their repair backlog.",
        "Our customers tell us kiln scheduling is their biggest headache.",
        "We already have studios using our reservation planner.",
        "I've interviewed parents about planning weekend outings.",
        "Our research shows families struggle to find walking companions.",
        "In my experience, gardeners waste time arranging seed swaps.",
        "We have helped families organize their neighborhood walks.",
    ]
    assert len(honest) == 15 and len(invented) == 8
    for sentence in honest:
        assert not claims.violations(sentence, founder_words=""), sentence
    for sentence in invented:
        assert claims.violations(sentence, founder_words=""), sentence
        assert not claims.violations(sentence, founder_words=sentence), sentence
        assert claims.violations(sentence, founder_words="I am building a new product."), sentence

    data = request.getfixturevalue("journey_db")
    _add(data, monkeypatch)
    _mail(data)
    draft = outreach.drafts(data["vid"], "draft")[0]
    body = draft["body"].replace("\n\n", "\n\n" + invented[0] + "\n\n", 1)
    with data["factory"]() as session:
        contact = session.get(GtmContact, draft["contact_id"])
        with pytest.raises(ValueError, match="gtm_draft_unsupported_claim"):
            validate_send(session, data["wid"], contact, body, outreach.founder_signature(data["vid"]), draft=draft | {"copy_origin": "model"})
        validate_send(session, data["wid"], contact, body, outreach.founder_signature(data["vid"]), draft=draft | {"copy_origin": "founder"})
        session.get(Venture, data["vid"]).raw_idea += " " + invented[0]
        session.commit()
    journey.validate_draft_copy(body, venture_id=data["vid"], generated=True)
    from app.chat import store
    monkeypatch.setattr(store, "list_messages", lambda *args, **kwargs: [{"role": "assistant", "body_md": invented[4]}])
    assert claims.violations(invented[4], founder_words=claims.founder_words(data["vid"]))
    monkeypatch.setattr(store, "list_messages", lambda *args, **kwargs: [{"role": "user", "body_md": invented[4]}])
    assert not claims.violations(invented[4], founder_words=claims.founder_words(data["vid"]))
    assert claims.violations(invented[4], founder_words="Do not say: " + invented[4])


@pytest.fixture(autouse=True)
def _app():
    load_company_os_app()


@pytest.mark.parametrize("description", [
    "We help hospital purchasing teams reconcile supplier invoices.",
    "We help birdwatchers arrange weekend outings.",
])
def test_community_opening_checks_the_product_phrase_not_firsthand_source(description):
    from app.autopilot import claims, outreach
    from app.gtm import playbooks

    product = outreach.product_phrase(description)
    text = playbooks.community_post({"who": "people here", "problem": "conflicting schedules"}, description)
    assert f"I'm building {product}." in text
    assert not claims.violations(text, founder_words="")
    unsafe = "An award-winning tool trusted by 500 customers."
    assert "I'm building" not in playbooks.community_post({}, unsafe)


@pytest.mark.parametrize("audience", ["hospital purchasing teams", "birdwatchers"])
def test_founder_evidence_keeps_saved_facts_when_chat_scope_is_unavailable(request, audience):
    from app.autopilot import claims
    from app.db.models import Venture
    from app.tenancy import bind, current
    from core.clock import utcnow
    from core.ids import new_id

    data = request.getfixturevalue("journey_db")
    venture_id = new_id("ven")
    stated = f"We have interviewed {audience}."
    scope = current()
    with data["factory"]() as session:
        session.add(Venture(id=venture_id, workspace_id=data["wid"], name="Evidence check", raw_idea=stated,
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    words = claims.founder_words(venture_id)
    assert stated in words and current() == scope
    assert not claims.violations(stated, founder_words=words)
    assert claims.violations(f"We have helped {audience}.", founder_words=words)
    with bind(workspace_id="unrelated-workspace", venture_id=venture_id, actor=scope.actor), pytest.raises(ValueError, match="chat_conversation_not_found"):
        claims.founder_words(venture_id)


@pytest.mark.parametrize("problem", ["missing supplier invoices", "arranging weekend outings"])
def test_outreach_prose_about_a_problem_is_not_a_structured_field(problem):
    from app.gtm.playbooks import copy_problem

    assert not copy_problem(f"I'm researching this problem: {problem}. What have you tried?")
    for body in (f"Problem: {problem}", f"A new tool. Offer: {problem}", f"Hi,\nProblem: {problem}"):
        assert copy_problem(body)

TEAM_PAGE = "# Meet our team\n\nJane Doe\nFounder\n\nSam Lee\nOffice Manager\n\nRiya Shah\nDental Hygienist\n"
LEAD = {"id": "l1", "kind": "business", "judging_status": "judged", "name": "Acme Dental", "company": "Acme Dental", "person": "Jane Doe",
        "quote": "Jane Doe, Founder. Our front desk manager reschedules every cancelled appointment by phone.",
        "source_url": "https://bakery.example/contact", "routes": []}
IDEA = "An AI receptionist that fills cancelled dental appointments from a waitlist. It texts patients automatically."


@pytest.mark.parametrize("audience,problem", [
    ("restaurant purchasing teams", "missing ingredient deliveries"),
    ("neighborhood swimmers", "finding swimming companions"),
])
def test_call_template_keeps_grounded_fit_question(audience, problem):
    from app.autopilot import claims
    from app.gtm import reachable

    lead = {"name": audience, "company": audience, "kind": "business", "evidence_label": "fit_only"}
    reachable._call_draft(lead, problem)
    for field in ("draft", "connection_note", "x_note"):
        assert "whether" in lead[field]
        assert lead[field].startswith(f"Hi {audience} team,")
        assert not claims.violations(lead[field], founder_words="")


def test_recipient_names_keep_typed_numbers_but_not_uniqueness_suffixes(journey_db):  # noqa: F811
    from app.autopilot import outreach
    from app.entry.service import _commit_idea
    from app.gtm import playbooks
    from app.intake.naming import unique_venture_name
    from app.tenancy import bind

    for base, idea in (("ValveDesk", "A tool for factory managers to coordinate equipment inspections."),
                       ("TrailNest", "An app for families to arrange woodland outings.")):
        for chosen, expected in ((base, base), (base, base), (base + " 3", base + " 3"),
                                 (base + " 3", base + " 3")):
            saved = unique_venture_name(journey_db["actor"], chosen)
            receipt = asyncio.run(_commit_idea(idea, options={"name": chosen, "founder_chosen_name": chosen,
                "defer_initial_clarification": True}, actor_id=journey_db["actor"], surface="test"))
            with bind(workspace_id=receipt["workspace"]["id"], venture_id=receipt["venture_id"], actor=journey_db["actor"]):
                assert receipt["workspace"]["name"] == saved
                assert outreach.product_phrase("", saved) == expected
                message = playbooks.messages(LEAD, "value_first", idea, venture_name=saved)
                assert f"building {expected}," in message["connection_note"]
                assert f"{expected} is still early" in outreach.followup_text(1, "Lee", saved)


@pytest.mark.parametrize("name,description,expected,summary", [
    ("KilnQueue", "It coordinates firing reservations for pottery studios.",
     "a tool that coordinates firing reservations for pottery studios", ""),
    ("KilnQueue", "Coordinate firing reservations for pottery studios.",
     "a tool to coordinate firing reservations for pottery studios", ""),
    ("KilnQueue", "We help pottery studios coordinate firing reservations.",
     "a tool to help pottery studios coordinate firing reservations", ""),
    ("SkyGather", "It matches amateur astronomers with nearby observing companions.",
     "a tool that matches amateur astronomers with nearby observing companions", ""),
    ("SkyGather", "Match amateur astronomers with nearby observing companions.",
     "a tool to match amateur astronomers with nearby observing companions", ""),
    ("SkyGather", "We help amateur astronomers find observing companions.",
     "a tool to help amateur astronomers find observing companions", ""),
    ("KilnQueue", "Not settled yet.", "", ""),
    ("SkyGather", "Not settled yet.", "", ""),
    ("KilnQueue", "I haven't chosen a focus.", "", ""),
    ("SkyGather", "I haven't chosen a focus.", "", ""),
    ("KilnQueue", "I want to start a reservation planner for pottery studios.",
     "a reservation planner for pottery studios", ""),
    ("SkyGather", "I want to start an observing companion finder for amateur astronomers.",
     "an observing companion finder for amateur astronomers", ""),
    ("KilnQueue", "KilnQueue is a reservation planner for pottery studios.",
     "a reservation planner for pottery studios", ""),
    ("SkyGather", "SkyGather is an observing companion finder for amateur astronomers.",
     "an observing companion finder for amateur astronomers", ""),
    ("KilnQueue", "KilnQueue coordinates firing reservations for pottery studios.",
     "a tool that coordinates firing reservations for pottery studios", ""),
    ("SkyGather", "SkyGather matches amateur astronomers with nearby observing companions.",
     "a tool that matches amateur astronomers with nearby observing companions", ""),
    ("KilnQueue", "Software that coordinates firing reservations for pottery studios.",
     "a tool that coordinates firing reservations for pottery studios", ""),
    ("SkyGather", "Software that matches amateur astronomers with nearby observing companions.",
     "a tool that matches amateur astronomers with nearby observing companions", ""),
    ("KilnQueue", "Not settled yet.", "a reservation planner for pottery studios",
     "A reservation planner for pottery studios."),
    ("SkyGather", "Not settled yet.", "an observing companion finder for amateur astronomers",
     "An observing companion finder for amateur astronomers."),
])
def test_outreach_templates_use_named_product_phrases(journey_db, monkeypatch, name, description, expected, summary):  # noqa: F811
    from app.autopilot import outreach
    from app.db.models import Venture
    from app.gtm import journey, playbooks
    from app.planning import execute
    from app.ventures.current_idea import current_revision

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    async def no_pitch(*_args, **_kwargs):
        return ""

    async def keep_message(_workspace_id, _venture_id, message, *_args, **_kwargs):
        return message

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(playbooks, "pitch", no_pitch)
    monkeypatch.setattr(playbooks, "compose", keep_message)
    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, description
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"playbooks": [{"key": "problem_interview"}],
            "reachable_leads": [LEAD | {"country": "CA"}]})
        session.commit()
    confirm_saved_idea(data["vid"])
    with data["factory"]() as session:
        revision = current_revision(session.get(Venture, data["vid"]), session)
        append_idea_revision(session, revision.id,
            structured=json.dumps({"offer": description, "summary": summary}), summary=summary)
        session.commit()

    _add(data, monkeypatch)
    _mail(data)
    phrase = name + (f", {expected}" if expected else "")
    saved = outreach.drafts(data["vid"], "draft")
    assert saved
    draft = saved[0]
    assert f"building {phrase}." in draft["body"]
    message = playbooks.for_lead(LEAD, [{"key": "problem_interview"}], outreach.venture_description(data["vid"]),
        venture_name=name)["playbook_messages"][0]
    for text in (draft["body"], message["email"], message["connection_note"], message["linkedin_dm"]):
        assert f"building {phrase}." in text
        assert description not in text
        assert ".." not in text and "\u2014" not in text
    assert len(message["connection_note"]) <= playbooks.NOTE_LIMIT
    assert outreach.OPT_OUT in draft["body"]
    assert "Your email address is listed publicly on" in draft["body"]
    assert "Business outreach · 1 Founder Street, New York, NY 10001" in draft["body"]
    assert "From: founder@gmail.example" in draft["body"]
    for text in (outreach._template(description, "Recipient", "", venture_name=name)[1],
                 playbooks.community_post({"who": "people here", "problem": "conflicting schedules"}, description, venture_name=name)):
        assert f"building {name}" in text and description not in text
    review = execute._template_drafts({"name": name, "pitch": summary or description,
        "problem": summary or description, "audience": "people here", "link": "", "founder": "Jordan"})
    assert f"exploring {phrase}." in review and description not in review


@pytest.mark.parametrize("person,company,product,problem", [
    ("Lee Rowan", "Factory purchasing", "A procurement planner", "Suppliers often deliver late"),
    ("Jo Bell", "Family cycling club", "A family cycling guide", "Parents struggle to choose safe routes"),
])
def test_conn_regression_linkedin_note_keeps_known_person_and_product(journey_db, monkeypatch, person, company, product, problem):
    from app.gtm import leads
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, journey_db["vid"]).name = product
        session.commit()
    monkeypatch.setattr(leads, "saved_leads", lambda *args: {"items": [{"id": "contact", "name": person,
        "company": company, "source_url": "", "status": "saved", "saved": {"evidence": {}}}]})
    reply = leads.linkedin_note(journey_db["wid"], journey_db["vid"], "contact", {"icp": {"problem": problem}})
    assert reply["note"].startswith(f"Hi {person.split()[0]},")
    assert product.lower() in reply["note"].lower()
    assert f"I'm building {problem}" not in reply["note"]


@pytest.fixture
def composition_db(request):
    import os

    return request.getfixturevalue("postgres_journey_db" if os.environ.get("TEST_POSTGRES_URL") else "journey_db")


@pytest.mark.parametrize("name,opening,request_text,person,company,domain,phrase", [
    ("BearingWatch",
     "I am a first-time hardware founder building wireless vibration sensors for plant managers at manufacturing plants in Texas. "
     "They attach a sensor to a motor, see early bearing faults and plan maintenance before an unplanned shutdown. "
     "I have a working hardware prototype, no website, no customer list and no sales. "
     "My first offer is a four-week paid pilot for $1,500 covering ten machines. "
     "My goal is one plant manager agreeing to pay for that pilot. I want to sell hardware, not build software.",
     "Draft one short email to Casey Sample at casey.sample@sample-auto.test using https://sample-auto.test/team. "
     "Ask whether he would pay $1,500 for a four-week vibration sensor pilot covering ten machines. "
     "Include the source disclosure, my mailing address and opt-out. Do not send yet.",
     "Casey Sample", "Sample Auto Works", "sample-auto.test",
     "a wireless vibration sensor that helps plant managers detect bearing faults and plan maintenance"),
    ("Trail Circle",
     "An app for weekend hikers helps them find nearby walking partners. "
     "I have only an idea, not a finished app. I want to test it with prospective users.",
     "Draft a short email to Clara Vogel about how she finds companions for weekend walks. "
     "I have only an idea, so do not imply I have a working app. Do not send it.",
     "Clara Vogel", "Local Walking Club", "walking.example",
     "an app that helps weekend hikers find nearby walking partners"),
], ids=["hardware-exact-founder-wording", "consumer-walking"])
def test_named_draft_describes_confirmed_product_and_shows_sender(
        composition_db, monkeypatch, name, opening, request_text, person, company, domain, phrase):
    from types import SimpleNamespace
    from tests.unit.credits.test_wallet_bridge import phase_marker
    from app.autopilot import outreach
    from app.credits import metered
    from app.credits.accounts import ensure_account
    from app.credits.policy import person_wallet_enabled
    from app.db.models import GtmContact, GtmEmailAccount, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import validate_send
    from app.gtm.relationships.service import _settings
    from app.tenancy import bind
    from core.clock import utcnow

    data, now, prompts = composition_db, utcnow(), []
    email = person.lower().replace(" ", ".") + "@" + domain
    lead = {"kind": "person", "judging_status": "judged", "person": person, "name": person,
        "company": company, "email": email, "source_url": f"https://{domain}/team",
        "quote": f"{person} shares their experience with planning and day-to-day coordination."}
    with data["factory"]() as session:
        phase_marker("person", session)
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, "An outdated product for an unrelated audience."
        session_id = data.get("sid", data["vid"] + "-idea")
        idea_session = session.get(IdeaSession, session_id)
        if idea_session is None:
            idea_session = IdeaSession(id=session_id, workspace_id=data["wid"], venture_id=data["vid"],
                case="know", state="clarifying", created_at=now, updated_at=now)
            session.add(idea_session)
            session.flush()
        revision = IdeaRevision(id=data.get("rid", data["vid"] + "-revision") + "-product", session_id=session_id,
            workspace_id=data["wid"], venture_id=data["vid"], rev=2, source="founder",
            structured=json.dumps({"offer": opening}), created_at=now)
        session.add(revision)
        session.flush()
        idea_session.current_revision_id = idea_session.confirmed_revision_id = revision.id
        idea_session.state, idea_session.confirmed_by_person_id, idea_session.confirmed_at = "confirmed", data["actor"], now
        session.add(GtmContact(id=data["vid"] + "-contact", workspace_id=data["wid"], email=email,
            email_confidence="safe", recipient_geo="US", lawful_basis="consent", lawful_basis_at=now,
            lawful_basis_note="You have recorded their permission for this research email.",
            source=f"venture:{data['vid']}:public", enrichment_json=json.dumps({"found_at": lead["source_url"],
                "business": company}), stage_changed_at=now, created_at=now))
        session.add(GtmEmailAccount(id=data["vid"] + "-sender", workspace_id=data["wid"],
            email=f"you@{domain}", composio_user_id=data["vid"] + "-fake-user",
            connected_account_id=data["vid"] + "-fake-gmail",
            status="active", created_at=now, updated_at=now))
        settings = _settings(session, data["wid"])
        settings.aup_accepted_at = settings.dpa_accepted_at = now
        settings.sender_postal_address = "1 Founder Street, New York, NY 10001"
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"playbooks": [{"key": "problem_interview"}], "reachable_leads": [lead,
            lead | {"person": "Sam Lee", "name": "Sam Lee", "email": f"other@{domain}",
                    "quote": "Sam Lee coordinates a different workflow."}]})
        session.commit()
        assert person_wallet_enabled(session)
    ensure_account(data["vid"], workspace_id=data["wid"], owner_person_id=data["actor"])
    monkeypatch.setattr(outreach, "site_url", lambda _vid: None)
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: pytest.fail("Draft composition must not send email"))
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"host": "smtp.sender.example", "port": 587,
            "user": f"you@{domain}", "sender_name": outreach.founder_signature(data["vid"]),
            "postal_address": "1 Founder Street, New York, NY 10001"}, {"password": "test-mail-password"})

    async def model(messages, **kwargs):
        prompts.append(messages)
        if "product phrase" in messages[0]["content"]:
            assert opening in messages[1]["content"]
            assert "outdated product" not in messages[1]["content"]
            content = phrase
        else:
            payload = json.loads(messages[1]["content"])
            assert request_text in payload["founder_request"]
            assert lead["quote"] in payload["evidence_from_their_public_page"]
            content = json.dumps({"subject": "Your experience", "body": payload["template"]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(metered, "llm", model)
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        assert journey._draft_ready_admitted(data["wid"], data["vid"], review_only=True,
            only={data["vid"] + "-contact"}, founder_request=request_text) == 1
        draft = outreach.drafts(data["vid"], "draft")[0]
        assert draft["body"].split("\n\n")[1].startswith(f"I'm building {name}, {phrase}.")
        assert draft["body"].startswith(f"Hi {person.split()[0]},\n")
        assert f"From: you@{domain}" in draft["body"]
        assert f"Your email address is listed publicly on {domain}." in draft["body"]
        from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT, unsubscribe_url
        link = unsubscribe_url(data["wid"], draft["contact_id"], draft["to"])
        assert outreach.OPT_OUT in draft["body"] and ("Stop these emails: " + link if link else REPLY_OPT_OUT) in draft["body"]
        assert "1 Founder Street" in draft["body"] and "—" not in draft["body"]
        assert draft["state"] == "draft" and draft["review_state"] == "draft"
        assert len(prompts) == 2
        with data["factory"]() as session:
            validate_send(session, data["wid"], session.get(GtmContact, draft["contact_id"]),
                draft["body"], outreach.founder_signature(data["vid"]))

        async def no_phrase(*_args, **_kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

        monkeypatch.setattr(metered, "llm", no_phrase)
        assert journey._draft_ready_admitted(data["wid"], data["vid"], review_only=True,
            only={draft["contact_id"]}, founder_request=request_text) == 0
        assert outreach.drafts(data["vid"], "draft") == [draft]
        retry = journey.draft_for_review(data["vid"], contact_id=draft["contact_id"], founder_request=request_text)
        assert retry["kind"] == "needs_you" and "Your draft is unchanged" in retry["body_md"]
        assert "Ask me to try again" in retry["body_md"] and outreach.drafts(data["vid"], "draft") == [draft]


@pytest.mark.parametrize("idea", ["A vendor invoice tracker", "A family walking planner"])
def test_outreach_copy_rejects_structured_fields_and_double_punctuation(idea):
    from app.gtm.playbooks import copy_problem

    assert not copy_problem(f"I'm building {idea}. Would you share how you handle this today?")
    for bad in (f"Offer: {idea}", f"Problem: {idea}", f"I'm building {idea}. ."):
        assert copy_problem(bad)


def test_people_come_from_a_public_team_page_with_quote_and_linkedin_search():
    from app.gtm import enrich, playbooks
    facts = enrich.extract("acme.example", [("https://acme.example/team", TEAM_PAGE)])
    people = playbooks.decision_makers(facts["people"], "Acme Dental")
    assert [(p["name"], p["role"]) for p in people] == [("Jane Doe", "Founder"), ("Sam Lee", "Office Manager")]
    assert people[0]["source_url"] == "https://acme.example/team" and "Jane Doe" in people[0]["quote"]
    assert people[0]["email"] is None  # no published address, none invented


def test_linkedin_link_is_a_people_search_never_a_profile_fetch():
    from app.gtm.playbooks import linkedin_search
    assert linkedin_search("Jane Doe", "Acme & Co") == "https://www.linkedin.com/search/results/people/?keywords=Jane%20Doe%20Acme%20%26%20Co"


@pytest.mark.parametrize("idea,audience", [("Vendor invoice checking", "Finance teams"), ("Weekend art sessions", "Hobby painters")])
def test_problem_interview_price_questions_are_optional_and_not_added_to_outreach(monkeypatch, idea, audience):
    from types import SimpleNamespace
    from app.credits import metered
    from app.experiments.price_band import PRICE_QUESTIONS
    from app.gtm import playbooks

    original = playbooks.messages({}, "problem_interview", idea)
    assert original["optional_questions"] == list(PRICE_QUESTIONS) and len(PRICE_QUESTIONS) == 4
    assert original["email"].count("?") == 1
    assert all(question not in original["email"] for question in PRICE_QUESTIONS)
    assert "optional_questions" not in playbooks.messages({}, "design_partner", idea)
    assert playbooks.fallback({})[0]["optional_questions"] == list(PRICE_QUESTIONS)
    assert playbooks.for_lead({}, [{"key": "problem_interview"}], idea)["playbook_messages"][0]["optional_questions"] == list(PRICE_QUESTIONS)

    async def selected(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"playbooks":[{"key":"problem_interview","why":"You are learning"},{"key":"value_first","why":"You can help"}]}'))])

    monkeypatch.setattr(metered, "llm", selected)
    monkeypatch.setattr(playbooks, "evidence", lambda venture: {})
    chosen = asyncio.run(playbooks.select("w", "v", {"who": audience}, idea))
    assert chosen[0]["optional_questions"] == list(PRICE_QUESTIONS)


def test_playbook_selection_falls_back_by_stage_when_the_model_fails(monkeypatch):
    from app.credits import metered
    from app.gtm import playbooks

    async def broken(*args, **kwargs):
        assert kwargs["purpose"] == "venture.chat.assistant" and kwargs["tier"] == "flash"
        raise RuntimeError("provider down")
    monkeypatch.setattr(metered, "llm", broken)
    monkeypatch.setattr(playbooks, "evidence", lambda vid: {"site_live": False, "has_users": False})
    chosen = asyncio.run(playbooks.select("w", "v", {"who": "dental clinic owners"}, IDEA))
    assert [item["key"] for item in chosen][0] == "problem_interview" and 2 <= len(chosen) <= 3 and all(item["why"] for item in chosen)
    assert playbooks.fallback({"site_live": True})[0]["key"] == "design_partner"
    assert playbooks.fallback({"has_users": True}, "startup founders")[1]["key"] == "founder_peer"


def test_connection_notes_fit_linkedin_limit_for_every_playbook():
    from app.gtm import playbooks
    long_idea = "A " + "very " * 80 + "long idea."
    for key in playbooks.PLAYBOOKS:
        message = playbooks.messages(LEAD, key, long_idea)
        assert len(message["connection_note"]) <= 300
        assert message["email"].startswith("Hi Jane,") and message["linkedin_dm"].startswith("Hi Jane,")
        assert message["subject"]


def test_page_titles_are_never_greeted_as_people():
    from app.gtm import playbooks
    titled = {"kind": "person", "judging_status": "pending", "name": "How We Run Our Clinic | Blog", "company": "", "quote": ""}
    assert playbooks.messages(titled, "problem_interview", IDEA)["email"].startswith("Hi there,")
    named_page = {"kind": "person", "judging_status": "judged", "name": "Best Dental Tips And Tricks", "company": "Smile Co", "quote": ""}
    assert playbooks.greeting(named_page) == "Smile Co team"
    assert playbooks.for_lead(named_page, playbooks.fallback({}), IDEA)["linkedin_search"] is None


@pytest.mark.parametrize("company,country,evidence,expected", [
    ("Oliver Reed", "GB", {"quote": "I'm Oliver Reed, a consultant helping factories plan their operations."}, "Oliver"),
    ("Clara Vogel", "DE", {"display_quote": "Ich bin Clara Vogel. Ich leite Bewegungskurse für Familien."}, "Clara"),
    ("Oliver Reed", "GB", {"quote": "Oliver Reed offers operations planning for manufacturers."}, "Oliver Reed team"),
    ("Clara Vogel", "DE", {"quote": "Clara Vogel bietet Bewegungskurse für Familien an."}, "Clara Vogel team"),
    ("Engineering Consultants", "GB", {"quote": "Our founder Oliver Reed oversees industrial planning."}, "Engineering Consultants team"),
    ("English Piano", "DE", {"title": "English Piano lessons with coach Clara Vogel"}, "English Piano team"),
    ("Oliver Reed", "GB", {"person": None, "title": "Oliver Reed | Owner"}, "Oliver"),
    ("Clara Vogel", "DE", {"person": None, "quote": "Clara Vogel, coach. Private movement sessions for families."}, "Clara"),
    ("Oliver Reed", "GB", {"person": None, "route_quote": "I am Oliver Reed. I advise factories. Email hello@solo.example."}, "Oliver"),
    ("Clara Vogel", "DE", {"route_display_quote": "Ich bin Clara Vogel. Kontakt: hello@solo.example"}, "Clara"),
    ("Lucie Martin", "FR", {"about": "Je suis Lucie Martin. Je propose des ateliers de jardinage."}, "Lucie"),
    ("Diego Ramos", "ES", {"snippet": "Soy Diego Ramos. Asesoro a empresas de transporte."}, "Diego"),
    ("Oliver Reed", "GB", {"quote": "My name is Oliver Reed. I advise factory owners."}, "Oliver"),
    ("Clara Vogel", "DE", {"text": "I’m Clara Vogel. I coach families in movement."}, "Clara"),
    ("Oliver Reed", "GB", {"quote": "Oliver Reed is a consultant for manufacturers."}, "Oliver"),
    ("Oliver Reed", "GB", {"title": "Oliver Reed | Operations consultant in Manchester"}, "Oliver"),
    ("Clara Vogel", "DE", {"title": "Clara Vogel, movement coach for families"}, "Clara"),
    ("Oliver Reed", "GB", {"quote": "Oliver Reed advises factories. Our owner is Lucie Martin."}, "Oliver Reed team"),
    ("Clara Vogel", "DE", {"quote": "Ich bin Clara Vogelmann. Kurse bei Clara Vogel."}, "Clara Vogel team"),
    ("Oliver Reed", "GB", {"quote": "I'm Oliver Reed-Smith. Services by Oliver Reed."}, "Oliver Reed team"),
    ("Engineering Consultants", "GB", {"title": "Engineering Consultants | Our founder Oliver Reed"}, "Engineering Consultants team"),
    ("English Piano", "DE", {"quote": "English Piano with coach Clara Vogel"}, "English Piano team"),
    ("Engineering Consultants", "GB", {"title": "Engineering Consultants | Founder Maya Singh"}, "Engineering Consultants team"),
    ("English Piano", "DE", {"title": "English Piano | Teacher Clara Vogel"}, "English Piano team"),
    ("Oliver Reed", "GB", {"person": None, "people": [{"quote": "Oliver Reed, operator. I advise factories.", "source_url": "https://solo.example/team"}]}, "Oliver"),
    ("Oliver Reed", "GB", {"title": "Oliver Reed", "quote": "Owner of a planning practice."}, "Oliver Reed team"),
    ("Clara Vogel", "DE", {"why": "Ich bin Clara Vogel", "role": "owner"}, "Clara Vogel team"),
    ("Oliver Reed", "GB", {"company": "", "person": None, "quote": "Oliver Reed, consultant"}, "Oliver"),
    ("Engineering Consultants", "GB", {"person": "Maya Singh", "quote": "Maya Singh, founder of our industrial practice."}, "Maya"),
])
def test_namesake_owners_need_adjacent_saved_public_evidence(journey_db, monkeypatch, company, country, evidence, expected):  # noqa: F811
    from app.autopilot import outreach
    from app.gtm import journey, playbooks, reachable

    route = {"kind": "email", "route_type": "published", "email": "hello@solo.example", "source_url": "https://solo.example/about",
        "quote": evidence.get("route_quote") or "Contact: hello@solo.example",
        "display_quote": evidence.get("route_display_quote") or "Contact: hello@solo.example"}
    lead = {"id": "solo", "kind": "business", "judging_status": "judged", "name": company, "company": company, "person": company,
        "country": country, "quote": "", "source_url": route["source_url"], "routes": [route], **evidence}
    assert playbooks.greeting(lead) == expected
    chosen = [{"key": "problem_interview"}]
    message = playbooks.for_lead(lead, chosen, "a way to simplify daily planning")["playbook_messages"][0]
    assert all(message[field].startswith(f"Hi {expected},") for field in ("email", "connection_note", "linkedin_dm"))

    data = journey_db
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    _mail(data)
    with data["factory"]() as session:
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"playbooks": chosen, "reachable_leads": [lead]})
        session.commit()
    assert journey._ingest(data["wid"], data["vid"], [reachable.contact_seed(lead)]) == 1
    assert journey._draft_ready_admitted(data["wid"], data["vid"], review_only=True) == 1
    draft = outreach.drafts(data["vid"], "draft")[0]
    assert draft["body"].startswith(f"Hi {expected},")
    assert "Your email address is listed publicly on solo.example." in draft["body"]
    assert outreach.OPT_OUT in draft["body"] and "1 Founder Street" in draft["body"]


def test_playbook_email_joins_review_batch_and_passes_send_gates(journey_db, monkeypatch):  # noqa: F811
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey, playbooks
    from app.gtm.relationships.email_compliance import validate_send
    data = journey_db
    with data["factory"]() as session:
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"playbooks": playbooks.fallback({}), "reachable_leads": [LEAD]})
        session.commit()
    _add(data, monkeypatch)
    _mail(data)
    draft = outreach.drafts(data["vid"], "draft")[0]
    assert draft["playbook"] == "problem_interview" and draft["requires_batch_approval"] and draft["subject"] == "How do you handle this today?"
    assert draft["body"].startswith("Hi Ada,") and "front desk manager" in draft["body"]
    with data["factory"]() as session:
        validate_send(session, data["wid"], session.get(GtmContact, draft["contact_id"]), draft["body"], outreach.founder_signature(data["vid"]))


def test_pitch_replaces_raw_founder_notes_and_rejects_bad_rewrites(monkeypatch):
    from types import SimpleNamespace
    from app.credits import metered
    from app.gtm import playbooks
    replies = iter(["a simple order tracker for home bakers who take cake orders on WhatsApp.",
                    "I'm building something for my mom idk"])

    async def fake(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))])
    monkeypatch.setattr(metered, "llm", fake)
    raw = "I'm building something for my mom's bakery orders on whatsapp idk Audience: her customers, its messy."
    line = asyncio.run(playbooks.pitch("w", "v", raw))
    assert line == "a simple order tracker for home bakers who take cake orders on WhatsApp"
    note = playbooks.messages({"company": "Rauf Home Cakes"}, "problem_interview", line)["connection_note"]
    assert "idk" not in note and "my mom" not in note and "order tracker for home bakers" in note
    assert asyncio.run(playbooks.pitch("w", "v", raw)) == ""


@pytest.mark.parametrize("code", ["model_not_configured", "provider_outcome_unknown", "provider_output_invalid", "provider_down"])
def test_pitch_falls_back_only_for_an_unconfigured_model(monkeypatch, code):
    from app.credits import metered
    from app.gtm import playbooks
    from llm_gateway.errors import ProviderCallError

    error = ProviderCallError(code, "private provider details")

    async def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(metered, "llm", unavailable)
    if code == "model_not_configured":
        assert asyncio.run(playbooks.pitch("w", "v", "An order tracker for home bakers")) == ""
    else:
        with pytest.raises(ProviderCallError) as caught:
            asyncio.run(playbooks.pitch("w", "v", "An order tracker for home bakers"))
        assert caught.value is error


@pytest.fixture(params=[
    ("LedgerLane", "Track overdue invoices for accounting firms", "accounting firms", "Cedar Accounts", "cedar-accounts.example",
     "We manually chase late invoices from clients every week."),
    ("TrailNotes", "Help families choose accessible weekend walks", "families planning weekend walks", "River Walk Club", "river-walks.example",
     "Our members share accessible walking routes for local families."),
])
def recipient_case(journey_db, monkeypatch, request):
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db
    name, idea, audience, business, domain, quote = request.param
    source = f"https://{domain}/about"
    icp = journey._icp(data["vid"]) | {"who": audience, "problem": idea, "confirmed": True}
    monkeypatch.setattr(journey, "_icp", lambda vid: dict(icp))
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = name, idea
        session.commit()
    assert journey._ingest(data["wid"], data["vid"], [{"organization": business, "source_url": source,
        "email": f"hello@{domain}", "enriched": True, "country": "US", "email_confidence": "published"}]) == 1
    with data["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == data["wid"]))
        contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note = "consent", utcnow(), "Agreed to receive this research email."
        session.commit()
    _mail(data)
    return data | {"name": name, "idea": idea, "quote": quote, "source_url": source,
        "lead": {"kind": "business", "name": business, "company": business, "source_url": source, "quote": quote},
        "page": f"https://{name.lower()}.example/waitlist"}


def _recipient_draft(data, monkeypatch, *, evidence_label="fit_only", has_page=True, has_evidence=True):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey

    monkeypatch.setattr(outreach, "site_url", lambda vid: data["page"] if has_page else None)
    with data["factory"]() as session:
        contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == data["wid"]))
        evidence = json.loads(contact.enrichment_json)
        evidence["evidence_label"] = evidence_label
        contact.enrichment_json = json.dumps(evidence)
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"pitch": data["idea"], "playbooks": [{"key": "problem_interview"}],
            "reachable_leads": [data["lead"] | {"quote": data["quote"] if has_evidence else ""}]})
        session.commit()
    journey.draft_for_review(data["vid"])
    return next(draft for draft in outreach.drafts(data["vid"], "draft") if draft.get("contact_id"))


def test_saved_body_rewrite_keeps_recipient_opt_out_and_compliance(recipient_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import OPT_OUT, sender_address, source_line, validate_send
    from app.gtm.relationships.unsubscribe import issue_unsubscribe_token

    data = recipient_case
    draft = _recipient_draft(data, monkeypatch)
    outreach._save_draft(data["vid"], draft | {"body": "Would you share your experience?"}, draft["doc_id"])
    saved = next(item for item in outreach.drafts(data["vid"], "draft") if item["doc_id"] == draft["doc_id"])
    assert OPT_OUT in saved["body"]
    assert saved["body"].count(issue_unsubscribe_token(data["wid"], draft["contact_id"], draft["to"])) == 1
    with data["factory"]() as session:
        contact = session.get(GtmContact, draft["contact_id"])
        assert source_line(contact) in saved["body"] and sender_address(session, data["wid"]) in saved["body"]
        identity = outreach.founder_signature(data["vid"])
        assert identity in saved["body"]
        validate_send(session, data["wid"], contact, saved["body"], identity)


@pytest.mark.parametrize("connected", [True, False])
def test_saved_draft_rewrite_keeps_only_a_connected_sender(recipient_case, monkeypatch, connected):
    from app.autopilot import outreach
    from app.connections import mailboxes
    from app.credits.policy import set_policy
    from app.db.models import GtmEmailAccount

    data = recipient_case
    set_policy("feature_flags", {"multi_gmail_v1": False}, "test")
    draft = _recipient_draft(data, monkeypatch)
    with data["factory"]() as session:
        sender = mailboxes.resolve_send_mailbox(session, data["wid"], venture_id=data["vid"])
        if not connected:
            session.get(GtmEmailAccount, sender["mailbox_id"]).status = "disconnected"
            session.commit()
    outreach._save_draft(data["vid"], draft | {"body": "Would you share how you handle this today?"}, draft["doc_id"])
    saved = next(item for item in outreach.drafts(data["vid"], "draft") if item["doc_id"] == draft["doc_id"])
    from app.core.local_mode import is_local
    assert (f"From: {sender['email']}" in saved["body"]) is (connected or is_local())
    assert saved["body"].count("From:") == int(connected or is_local())
    if is_local() and not connected:
        with data["factory"]() as session, pytest.raises(mailboxes.MailboxUnavailable):
            mailboxes.resolve_send_mailbox(session, data["wid"], venture_id=data["vid"])
    assert saved["state"] == "draft" and saved["review_state"] == "draft"


def test_draft_saved_before_the_link_existed_gains_it_and_can_be_validated(recipient_case, monkeypatch):
    import json

    from app.autopilot import outreach
    from app.db.models import GtmContact, VentureDocument
    from app.gtm.relationships.email_compliance import validate_send
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    data = recipient_case
    draft = _recipient_draft(data, monkeypatch)
    link = unsubscribe_url(data["wid"], draft["contact_id"], draft["to"])
    with data["factory"]() as session:
        row = session.get(VentureDocument, draft["doc_id"])
        saved = json.loads(row.provenance_json)
        saved["body"] = saved["body"].replace("Stop these emails: " + link, "").strip()
        assert link not in saved["body"]
        row.provenance_json = json.dumps(saved)
        session.commit()
    read = next(item for item in outreach.drafts(data["vid"], "draft") if item["doc_id"] == draft["doc_id"])
    assert read["body"].count(link) == 1
    with data["factory"]() as session:
        validate_send(session, data["wid"], session.get(GtmContact, draft["contact_id"]), read["body"], outreach.founder_signature(data["vid"]))


@pytest.mark.parametrize("evidence_label", ["fit_only", "problem_observed"])
def test_recipient_source_stays_in_metadata_and_review_not_email(recipient_case, monkeypatch, evidence_label):
    from types import SimpleNamespace
    from app.autopilot import outreach
    from app.chat import customers
    from app.credits import metered
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, sender_address, validate_send

    data = recipient_case
    draft = _recipient_draft(data, monkeypatch, evidence_label=evidence_label)
    assert "Source:" not in draft["body"] and data["source_url"] not in draft["body"]
    assert draft["source_url"] == data["source_url"]
    review = next(item for item in journey.status(data["wid"], data["vid"])["drafts"] if item.get("contact_id"))
    assert review["source_url"] == data["source_url"] and review["body"] == draft["body"]
    async def shorten(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Would you share your experience?"))])
    monkeypatch.setattr(metered, "llm", shorten)
    shortened = asyncio.run(customers._shorter(data["vid"], data["wid"], draft))
    assert "Source:" not in shortened and data["page"] in shortened
    with data["factory"]() as session:
        contact = session.get(GtmContact, draft["contact_id"])
        identity = outreach.founder_signature(data["vid"])
        body = complete_body(session, data["wid"], contact, draft["body"] + f"\n\nSource: {data['source_url']}\nSource:", identity)
        assert "Source:" not in body and OPT_OUT in body and sender_address(session, data["wid"]) in body
        assert identity in shortened and OPT_OUT in shortened and sender_address(session, data["wid"]) in shortened
        validate_send(session, data["wid"], contact, body, identity)
        with pytest.raises(ValueError, match="gtm_source_disclosure_required"):
            validate_send(session, data["wid"], contact, draft["body"] + f"\n\nSource: {data['source_url']}", identity)
    journey.patch_draft(data["wid"], data["vid"], review["doc_id"], {"decision": "approved", "expected_revision": review["revision"]})


@pytest.mark.parametrize("has_page", [True, False])
def test_public_page_is_a_sentence_or_omitted(recipient_case, monkeypatch, has_page):
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    data = recipient_case
    draft = _recipient_draft(data, monkeypatch, has_page=has_page)
    body = draft["body"]
    link = unsubscribe_url(data["wid"], draft["contact_id"], draft["to"])
    assert body.count(link) == 1
    assert "Product:" not in body
    if has_page:
        assert f"You can see what I'm building here: {data['page']}" in body
    else:
        assert "http" not in body.replace(link, "") and "You can see what I'm building here:" not in body


@pytest.mark.parametrize("founder_name", ["", "mira Patel"])
def test_signature_and_recipient_lines_start_capitalized(recipient_case, monkeypatch, founder_name):
    from app.autopilot import outreach
    from app.db.models import Person, Workspace
    from app.gtm.relationships.email_compliance import OPT_OUT, sender_address

    data = recipient_case
    with data["factory"]() as session:
        owner = session.get(Person, session.get(Workspace, data["wid"]).owner_id)
        owner.display_name = founder_name
        session.commit()
    body = _recipient_draft(data, monkeypatch)["body"]
    expected = "Mira" if founder_name else data["name"]
    assert outreach.founder_signature(data["vid"]) == expected and f"\n\n{expected}\n\n" in body
    assert all(not line[:1].islower() for line in body.splitlines() if line)
    with data["factory"]() as session:
        assert OPT_OUT in body and sender_address(session, data["wid"]) in body


@pytest.mark.parametrize("display_name", ["Founder", "mira Patel"])
def test_drafts_sign_with_owner_saved_first_name(recipient_case, monkeypatch, display_name):
    from app.api.greeting_api import _update
    from app.authz.workspace import ensure_subject_person
    from app.autopilot import outreach
    from app.chat import assistant
    from app.db.models import Person, Workspace
    from app.planning import execute
    from app.ventures import state_brief

    data = recipient_case
    with data["factory"]() as session:
        owner = session.get(Person, session.get(Workspace, data["wid"]).owner_id)
        owner.display_name = display_name
        owner_id = owner.id
        session.commit()
    _update(owner_id, first_name="Nadiya")
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"sender_name": "Nadiya"}, {})
    ensure_subject_person("another-founder")
    _update("another-founder", first_name="Someone Else")
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: pytest.fail("Drafting must not send"))
    assert outreach.founder_signature(data["vid"]) == "Nadiya"
    draft = _recipient_draft(data, monkeypatch)
    assert "\n\nNadiya\n\n" in draft["body"]
    assert f"\n\n{data['name']}\n\n" not in draft["body"]

    async def complete(messages, **kwargs):
        assert "Sign as: Nadiya." in messages[1]["content"]
        return ""

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": data["name"], "offer": data["idea"], "audience": data["idea"]}})
    review = asyncio.run(execute._review_drafts(data["vid"], "Draft outreach emails", ""))
    assert "\nNadiya" in review and "Someone Else" not in review


@pytest.mark.parametrize("has_evidence", [True, False])
def test_observation_uses_public_evidence_or_is_omitted(recipient_case, monkeypatch, has_evidence):
    from app.autopilot import outreach
    from app.gtm import playbooks, reachable
    from app.gtm.relationships.email_compliance import sender_address

    data = recipient_case
    body = _recipient_draft(data, monkeypatch, has_evidence=has_evidence)["body"]
    assert "Does the problem I'm researching affect your work?" in body
    assert f"Your email address is listed publicly on {data['source_url'].split('/')[2]}." in body
    with data["factory"]() as session:
        assert outreach.OPT_OUT in body and sender_address(session, data["wid"]) in body
    lead = data["lead"] | {"quote": data["quote"] if has_evidence else "", "evidence_label": "fit_only"}
    reachable._call_draft(lead, data["idea"])
    message = playbooks.messages(lead, "problem_interview", data["idea"])["email"]
    for copy in (body, lead["draft"], message):
        assert "I found " not in copy
        assert ('I read this on your public page: "' in copy) is has_evidence
        if has_evidence:
            assert playbooks._observation(lead) in copy
    assert "I found " not in outreach._template(data["idea"], lead["company"], "")[1]

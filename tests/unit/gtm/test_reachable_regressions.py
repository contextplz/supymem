"""Production reachable-lead regressions; synthetic receipts, no browser or network."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.tasks.test_skills import receipt_db, _gtm, _verify
from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_journey_signals import quality_response
from tests.unit.tasks.test_task_lifecycle import lifecycle, _admit, _rows


@pytest.mark.parametrize("field", ["reachable_leads", "channels", "need_signals"])
def test_saved_reachable_results_complete_research(receipt_db, field):
    from app.db.models import VentureDocument
    from app.tasks import verification
    payload = _gtm(receipt_db, prospect=False)
    _, session, _ = receipt_db
    document = session.get(VentureDocument, payload["journey_id"])
    data = json.loads(document.provenance_json)
    data[field] = [{"name": "Stonehouse Veterinary Services Ltd", "quote": "Independent UK pet practice", "source_url": "https://clinic.example/"}]
    document.provenance_json = json.dumps(data)
    result = _verify(receipt_db, "gtm.journey.prepare", payload)
    assert result["verified"] and not result["blocker"]
    assert result["evidence"][0][field + "_count"] == 1
    task = SimpleNamespace(venture_id="ven_skills", workspace_id="ws_skills")
    assert "Independent UK pet practice" in verification._artifact(session, task, None, "venture_document:" + document.id)


@pytest.mark.parametrize("field", [None, "reachable_leads", "industry_experts", "channels", "need_signals", "prospects", "suppressed", "unreachable"])
def test_completed_customer_task_points_to_reachable_review_only_with_leads(receipt_db, field):
    from app.db.models import GtmSuppression, VentureDocument
    from app.tasks.service import action
    from app.ventures.run_status import next_step
    payload = _gtm(receipt_db, prospect=field == "prospects")
    _, session, _ = receipt_db
    GtmSuppression.__table__.create(session.get_bind(), checkfirst=True)
    if field and field != "prospects":
        document = session.get(VentureDocument, payload["journey_id"])
        data = json.loads(document.provenance_json)
        key = "reachable_leads" if field in {"suppressed", "unreachable"} else field
        data[key] = [{"id": "fixture_result", "value_exchange": "A useful customer conversation" if field != "unreachable" else "",
            "suppressed": field == "suppressed"}]
        document.provenance_json = json.dumps(data)
    session.flush()
    task = SimpleNamespace(state="completed", venture_id="ven_skills", workspace_id="ws_skills",
        scope_json='{"steps":[{"skill":"gtm.journey.prepare"}]}')
    review = action(task, [SimpleNamespace(skill="gtm.journey.prepare")], session=session)
    has_leads = field in {"reachable_leads", "industry_experts", "prospects"}
    label = "Review reachable leads" if has_leads else "Find customers"
    step = "review" if has_leads else "prepare"
    assert review == {"label": label, "href": f"/gtm?venture=ven_skills&step={step}"}
    assert next_step({"state": "completed", "action": review}, "gtm.journey.prepare") == label
    assert next_step({"state": "completed"}, "gtm.journey.prepare") == "Find customers"


def test_completed_customer_task_ignores_other_venture_leads(receipt_db):
    from app.db.models import GtmContact, VentureDocument
    from app.tasks.service import action
    _, session, now = receipt_db
    _gtm(receipt_db, prospect=False)
    session.add(GtmContact(id="other_contact", workspace_id="ws_skills", source="venture:venXskills:public",
        created_at=now, stage_changed_at=now))
    session.add(VentureDocument(id="other_journey", venture_id="other_venture", kind="gtm_journey", title="Other leads",
        provenance_json='{"reachable_leads":[{"id":"other_lead"}]}', created_at=now, updated_at=now))
    session.flush()
    task = SimpleNamespace(state="completed", venture_id="ven_skills", workspace_id="ws_skills",
        scope_json='{"steps":[{"skill":"gtm.journey.prepare"}]}')
    assert action(task, session=session) == {"label": "Find customers", "href": "/gtm?venture=ven_skills&step=prepare"}


@pytest.mark.parametrize("audience, market_type, label", [
    ("Finance teams", "B2B", "Review reachable leads"), ("Weekend campers", "B2C", "Find customers"),
])
def test_completed_customer_task_can_read_leads_without_a_session(receipt_db, monkeypatch, audience, market_type, label):
    from app.db.models import GtmSuppression, VentureDocument
    from app.tasks.service import action
    from sqlalchemy.orm import sessionmaker

    payload = _gtm(receipt_db)
    _, session, _ = receipt_db
    engine = session.get_bind()
    GtmSuppression.__table__.create(engine, checkfirst=True)
    document = session.get(VentureDocument, payload["journey_id"])
    document.provenance_json = json.dumps({**json.loads(document.provenance_json),
        "icp": {"who": audience, "market_type": market_type}})
    session.commit()
    monkeypatch.setattr("app.state.db", lambda: sessionmaker(engine))
    task = SimpleNamespace(state="completed", venture_id="ven_skills", workspace_id="ws_skills",
        scope_json='{"steps":[{"skill":"gtm.journey.prepare"}]}')
    assert action(task)["label"] == label


@pytest.mark.parametrize("name,quote,url", [
    ("Clinic", "Independent UK veterinary practice", "https://clinic.example/team"),
    ("Trail association", "Organizes guided walks for weekend hikers", "https://trail.example/walks"),
])
def test_reachable_receipt_completes_task_and_clears_attention(lifecycle, name, quote, url):
    from app.db.models import GtmContact, GtmSuppression, Job, VentureDocument
    from app.db.task_models import TaskStep
    data = lifecycle
    with data["factory"]() as session:
        for model in (GtmContact, GtmSuppression, VentureDocument):
            model.__table__.create(session.get_bind(), checkfirst=True)
    job_id = _admit(data, kind="gtm.journey.prepare")
    with data["factory"]() as session:
        payload = {"journey_id": "reachable_receipt", "prepare_id": "reachable_prepare"}
        session.get(Job, job_id).payload_json = json.dumps(payload)
        session.query(TaskStep).filter_by(job_id=job_id).one().inputs_json = json.dumps(payload)
        now = data["clock"][0].replace(tzinfo=None)
        session.add(VentureDocument(id=payload["journey_id"], venture_id="ven_lifecycle", kind="gtm_journey", title="Reachable leads",
            provenance_json=json.dumps(payload | {"job_id": job_id, "run_id": payload["prepare_id"], "state": "ready",
                "reachable_leads": [{"name": name, "quote": quote, "source_url": url, "value_exchange": "Discuss booking workflows"}]}),
            created_at=now, updated_at=now))
        session.flush()
        data["service"].after_job(session, job_id)
        session.commit()
    task, run, steps = _rows(data)
    assert task.state == run.state == steps[0].state == "completed"
    assert task.blocker_json is None
    with data["factory"]() as session:
        assert data["service"].action(task, steps, session=session)["label"] == "Review reachable leads"


@pytest.mark.parametrize("prefix", ["Accept:", "Reject -", "Accepted:", "Yes:", "No —", "Accept at group level:"])
def test_judge_verdict_is_not_a_value_sentence(prefix):
    from app.gtm.discovery import Judgement
    row = Judgement(id=0, kind="business", quote="Independent UK pet practice", affiliation="", name="Clinic", url="https://clinic.example/",
        why=prefix + " independent UK pet practice handles bookings.", value_exchange=prefix + " independent UK pet practice handles bookings.")
    assert row.why == row.value_exchange == "independent UK pet practice handles bookings."


@pytest.mark.parametrize("mode,audience,role,topic", [
    ("B2B", "Warehouse operations managers", "Operations Manager", "shelving and shift planning"),
    ("B2C", "Home bakers", "Baking Club Organiser", "theme cakes and recipe sharing"),
])
def test_person_explanations_do_not_guess_gender(journey_db, monkeypatch, mode, audience, role, topic):
    import asyncio
    import re
    from app.credits import metered as credits_metered
    from app.gtm import journey, playbooks
    from app.gtm.discovery import Judgements, parse_response
    from app.gtm.supymem.draft import _prompt
    from app.research import metered
    from tests.unit.gtm.test_journey_signals import _leased_payload

    quote = f"Morgan Reed is a {role}. She discusses {topic}."
    neutral = f"Their experience with {topic} could help you learn."
    row = {"id": 0, "kind": "person", "name": "Morgan Reed", "role": role, "affiliation": "",
        "url": "https://public.example/morgan", "quote": quote, "fit": 90, "why": neutral, "value_exchange": neutral}
    for explanation in (neutral, "He coordinates the group.", "You could help him.", "HIS work is public.",
            "She's active in the group.", "Ask her about the group.", "Her group's work is public."):
        checked = parse_response(Judgements, json.dumps({"items": [row | {"why": explanation, "value_exchange": explanation}]}))["items"][0]
        assert checked["quote"] == quote and checked["fit"] == 90
        for field in ("why", "value_exchange"):
            assert checked[field] and not re.search(r"\b(?:he|him|his|she|her)\b", checked[field], re.I)
            if explanation == neutral:
                assert checked[field] == neutral

    instructions = []
    def synthesize(*args, **kwargs):
        instructions.append(kwargs["messages"][0]["content"])
        return {"items": []}
    monkeypatch.setattr(metered, "synthesize", synthesize)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience)
    payload = _leased_payload(journey_db)
    context = {"mode": mode, "audience": audience, "candidates": [row | {"text": quote}]}
    for stage in ("reachjudge", "reachdraft"):
        asyncio.run(journey._discovery_model(payload, stage, context))
    async def complete(messages, **kwargs):
        instructions.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    monkeypatch.setattr(credits_metered, "llm", complete)
    message = playbooks.messages(row, "problem_interview", topic)
    asyncio.run(playbooks.compose(journey_db["wid"], journey_db["vid"], message, quote))
    instructions.append(_prompt({"name": row["name"], "title": role}, 1))
    assert len(instructions) == 4
    assert all("name, role, or they/them/their" in instruction and "Never infer gender" in instruction for instruction in instructions)


@pytest.mark.parametrize("name", ["Stonehouse Veterinary Services Ltd", "Stonehouse Limited", "Stonehouse LLP", "Stonehouse PLC", "Stonehouse CIC", "Stonehouse Group", "Stonehouse Partnership Ltd"])
def test_company_name_never_gets_sole_trader_note(name):
    from app.gtm.reachable import outreach_hint
    hint = outreach_hint("GB", business_name=name)
    assert "sole trader" not in hint["note"].lower()
    assert hint["subscriber"] != "corporate"


def test_registry_number_and_plain_sole_trader_note():
    from app.gtm.reachable import outreach_hint
    assert not outreach_hint("GB", company_number="12345678")["note"]
    assert not outreach_hint("GB", {"url": "https://registry.example/company/123"})["note"]
    assert outreach_hint("GB")["note"] == "UK note: if this is a sole trader, ask permission before sending marketing email."


def test_email_display_quote_is_text_but_verification_keeps_exact_html():
    from app.gtm import enrich, reachable
    raw = '<p>Contact &amp; enquiries <a href="mailto:info@clinic.example" class="wixui-rich-text__text">info@clinic.example</a>&nbsp;today</p>'
    route = reachable.contact_routes(enrich.extract("clinic.example", [("https://clinic.example/", raw)]))[0]
    assert route["quote"] in raw
    assert route["display_quote"] == "Contact & enquiries info@clinic.example today"


def test_u73_vendor_is_rejected_before_streaming_but_restaurant_is_not(journey_db):
    from app.gtm import journey, reachable
    context = {"competitors": [], "offer": "Restaurant rota scheduling software", "problem": "Managers fixing shifts over text", "audience": "Restaurant GMs in the US"}
    text = "Restaurant365 restaurant management software. Restaurant scheduling solutions. Book a demo."
    assert journey._same_offer("https://upserve.example/", "Upserve – Restaurant365", text, [], context["offer"], context["problem"])
    assert reachable._pending({"url": "https://upserve.example/", "title": "Upserve – Restaurant365", "text": text}, context) is None
    assert not journey._same_offer("https://bistro.example/", "Independent Bistro", "Our team uses scheduling software to fill shifts. Book your table.", [], context["offer"], context["problem"])


def test_u74_fit_quote_omits_reservation_boilerplate_and_preserves_staffing():
    from app.gtm.enrich import fit_excerpt
    boilerplate = "We recommend making a reservation ahead of time. Book your table today!"
    assert fit_excerpt(boilerplate) == ""
    assert fit_excerpt(boilerplate + " Our team of 24 staff rotates shifts weekly.") == "Our team of 24 staff rotates shifts weekly."


def test_u75_contact_route_removes_nav_but_keeps_verification_quote():
    from app.gtm import enrich, reachable
    raw = "Book Table Skip to main content (202)-555-0146 Menu Drinks Specials Events Order Reserve Catering Facebook page Instagram page"
    route = reachable.contact_routes({"contacts": [], "phone": {"value": "(202)-555-0146", "quote": raw, "source_url": "https://bistro.example/contact"}})[0]
    assert route["quote"] == raw
    assert route["display_quote"] == "(202)-555-0146"
    assert enrich.contact_excerpt("Reservations only. Call (202)-555-0146 for staff enquiries. Menu", route["value"]) == "Call (202)-555-0146 for staff enquiries."


@pytest.mark.parametrize("name,email,host", [("Wholesale parts", "Team@Wholesale.Example", "wholesale.example"),
                                          ("Gardening classes", "GardeningFixture@gardening.example", "gardening.example")])
def test_contact_excerpts_remove_svg_attributes_and_footer(journey_db, name, email, host):
    from app.gtm import enrich, reachable
    raw = f'<svg><path d="M.5 62.9-34.5 67.2 0 79.7 44.3 79.7 101.9V416z"></path></svg><p>Contact {name}: <a href="mailto:{email}" aria-label="Email 1">{email}</a></p><footer>All Rights Reserved. Terms &amp; Conditions · Privacy Policy · Refund Policy · Partner With Us</footer>'
    facts = enrich.extract(host, [(f"https://{host}/contact", raw)], published_mailboxes=True)
    route = reachable.contact_routes(facts)[0]
    assert route["email"] == email.lower() and route["quote"] in raw
    assert route["display_quote"].casefold() == f"Contact {name}: {email}".casefold()
    truncated = f'.5 62.9-34.5 67.2 0 79.7 44.3 79.7 101.9V416z"> <a href="mailto:{email}" aria-label="Email 1">{email}</a>'
    assert enrich.contact_excerpt(truncated, email).casefold() == email.casefold()


def test_saved_lead_has_one_ask_one_closing_and_clean_display(journey_db):
    from app.db.models import VentureDocument
    from app.gtm import journey
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], "Physiotherapy clinics in Bangalore", problem="A tool for appointment reminders")
    raw = '.5 62.9-34.5 67.2 0 79.7 44.3 79.7 101.9V416z"> <a href="mailto:CareStudio@Consumer.Example" aria-label="Email 1">CareStudio@Consumer.Example</a> All Rights Reserved. Privacy Policy · Partner With Us'
    draft = "Hi Sample Physical Therapy team, I'm researching appointment reminders. Would you be open to a 15-minute call? Happy to share my findings. Reply with a time."
    with data["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == "gtm_journey"))
        document = json.loads(row.provenance_json)
        document["reachable_leads"] = [{"id": "quantum", "name": "Sample Physical Therapy", "company": "Sample Physical Therapy", "role": "", "location": "Bangalore", "fit": 90, "why": "Published physiotherapy clinic", "quote": raw, "source_url": "https://sample-physical-therapy.test/contact", "routes": [{"kind": "email", "email": "carestudio@consumer.example", "source_url": "https://sample-physical-therapy.test/contact", "quote": raw}], "draft": draft, "connection_note": "Open to a 15-minute call? Reply with a time.", "draft_source": "template"}]
        row.provenance_json = json.dumps(document)
        session.commit()
    for _ in range(2):
        lead = journey.status(data["wid"], data["vid"])["reachable_leads"][0]
        assert lead["draft"] == draft.replace("team, ", "team,\n\n")
        for field in ("draft", "connection_note", "x_note"):
            assert lead[field].count("15-minute call") == 1 and lead[field].lower().count("reply with a time") == 1
        assert lead["routes"][0]["display_quote"].lower() == "carestudio@consumer.example"
        assert "V416z" not in lead["display_quote"]
    journey.update_settings(data["wid"], data["vid"], {"booking_link": "https://cal.com/founder/15min"})
    lead = journey.status(data["wid"], data["vid"])["reachable_leads"][0]
    for field in ("draft", "connection_note", "x_note"):
        assert lead[field].count("15-minute call") == 1
        assert lead[field].count("Choose a time: https://cal.com/founder/15min") == 1
        assert "reply with a time" not in lead[field].lower()


@pytest.mark.parametrize("outcome", ["personalised", "uncertain", "offline"])
@pytest.mark.hosted_only
def test_business_team_personalisation_uses_real_metered_provider_and_one_receipt(journey_db, monkeypatch, outcome):
    import asyncio
    from app.gtm import journey, reachable
    from app.db.models import CreditAccount
    from tests.idea_fixtures import install_provider
    from tests.unit.gtm.test_journey_signals import _leased_payload
    data = journey_db
    audience = "Physiotherapy clinics in Bangalore"
    journey.update_icp(data["wid"], data["vid"], audience, problem="A tool for appointment reminders")
    payload = _leased_payload(data)
    original = journey._discovery_model
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage != "reachjudge":
            return await original(payload, stage, context)
        return {"items": [{"id": row["id"], "kind": "business", "quote": "Physiotherapy clinic in Bangalore", "affiliation": "", "name": "Sample Physical Therapy", "url": row["url"], "fit": 90, "why": "Published physiotherapy clinic managing appointments."} for row in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    def response(messages, options):
        assert options["purpose"] == "gtm.discovery.draft"
        candidates = json.loads(messages[-1]["content"])["candidates"]
        assert "Sample Physical Therapy team" in candidates[0]["recipient_name"]
        if outcome == "uncertain":
            raise TimeoutError()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"items": [{"id": candidates[0]["id"], "body": "Hi Sample Physical Therapy team, your published clinic work in Bangalore caught my attention. How do you handle appointment follow-ups? Open to a 15-minute call? Reply with a time.", "connection_note": "Hi Sample Physical Therapy team, could we discuss appointment follow-ups? Reply with a time."}]})), finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=300, completion_tokens=100, total_tokens=400), model="DeepSeek-V4-Pro")
    calls = install_provider(monkeypatch, response)
    if outcome == "offline":
        monkeypatch.setenv("LLM_OFFLINE", "1")
    url = "https://sample-physical-therapy.test/"
    candidate = {"id": 0, "kind": "business", "url": url, "title": "Sample Physical Therapy", "text": "Sample Physical Therapy\nPhysiotherapy clinic in Bangalore", "pages": [{"url": url, "text": "Physiotherapy clinic in Bangalore"}], "routes": [{"kind": "email", "email": "carestudio@consumer.example", "source_url": url, "quote": "carestudio@consumer.example"}], "people": [], "country": "IN"}
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc["_runtime"].update(reachable_candidates_quality_v1=[candidate]))
    context = {"mode": "B2B", "audience": audience, "problem": "A tool for appointment reminders", "competitors": [], "offer": ""}
    async def run():
        first = await reachable.discover(None, [], [], context, payload)
        second = await reachable.discover(None, [], [], context, payload)
        return first, second
    first, second = asyncio.run(run())
    assert first[0]["draft_source"] == second[0]["draft_source"] == ("model" if outcome == "personalised" else "template")
    assert first[0]["draft_status"] == {"personalised": "personalised", "uncertain": "outcome_uncertain", "offline": "provider_unavailable"}[outcome]
    assert len(calls) == int(outcome != "offline")
    with data["factory"]() as session:
        assert (session.get(CreditAccount, data["account"]).spent_micro > 0) == (outcome == "personalised")
    operation = journal.operation(payload["journey_id"], "discovery-reachdraft-0")
    if outcome != "offline":
        assert operation["state"] == ("completed" if outcome == "personalised" else "uncertain")
    if outcome == "personalised":
        assert operation["usage"]["settled"]
    if outcome == "offline":
        assert not journal.document(payload["journey_id"])["_runtime"]["calls_used"].get("discovery-reachdraft-0")
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc["_runtime"]["calls_used"].update({"discovery-reachdraft-0": 1}))
        monkeypatch.setenv("LLM_OFFLINE", "0")
        third = asyncio.run(reachable.discover(None, [], [], context, payload))
        assert third[0]["draft_source"] == "model" and len(calls) == 1


def test_specialist_inboxes_are_excluded_and_people_then_general_contacts_rank_first():
    from app.gtm import enrich, reachable
    excluded = "careers jobs job recruitment recruit vacancies hr feedback complaints privacy dpo gdpr data accounts billing invoices finance payroll noreply no-reply donotreply abuse webmaster postmaster press media marketing-unsubscribe".split()
    raw = "\n".join(local + "@clinic.example" for local in excluded + ["support", "info", "jane"])
    raw += '\n<p>Jane Smith, Practice Manager jane@clinic.example</p><form><input type="email"><textarea></textarea></form>'
    routes = reachable.contact_routes(enrich.extract("clinic.example", [("https://clinic.example/contact", raw)]))
    assert [route.get("email") for route in routes if route["kind"] == "email"] == ["jane@clinic.example", "info@clinic.example", "support@clinic.example"]


def test_deepseek_draft_tolerates_fences_extra_keys_and_minor_overflow_without_losing_safety():
    from app.gtm.discovery import ReachDrafts, parse_response
    body = ("Hi Jane, I came across your independent veterinary practice while researching how UK clinics handle appointment bookings and follow-ups. "
        "I'm exploring whether a small tool could make that work easier, but I don't want to assume you have a problem or need another system. "
        "I'd really value hearing how your team handles this today, what works well, and whether anything is frustrating for you. "
        "Would you be open to a short, fifteen-minute research conversation next week? There's no product demo or commitment involved. "
        "If you'd like to chat, please reply with a time that suits you.")
    note = ("Hi Jane, I came across your independent veterinary practice while researching how UK clinics handle appointment bookings and follow-ups. "
        "I'd value hearing how your team handles this today, without assuming you need another tool. "
        "Would you be open to a 15-minute research conversation? Please reply with a time.")
    response = {"status": "ok", "items": [{"id": "0", "body": body, "connection_note": note, "reasoning": "Published clinic workflow."},
        {"id": 1, "body": "Hi Sam, could we discuss appointment follow-ups? Reply with a time.", "connection_note": "Hi Sam, open to a 15-minute call? Reply with a time."},
        {"id": "2", "body": body + " Email guessed@example.com", "connection_note": note},
        {"id": "3", "body": "Trusted by 1000 customers.", "connection_note": note},
        {"id": "4", "body": "word " * 111, "connection_note": note}]}
    result = parse_response(ReachDrafts, "```JSON\n" + json.dumps(response) + "\n```")
    assert [row["id"] for row in result["items"]] == [0, 1]
    assert len(result["items"][0]["body"].split()) <= 90
    assert len(result["items"][0]["connection_note"]) <= 300


@pytest.mark.parametrize("personalised", [True, False])
def test_uk_research_keeps_model_drafts_and_lowercases_template_topic(journey_db, monkeypatch, personalised):
    import asyncio
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    traces = []
    monkeypatch.setattr(reachable, "trace", lambda *args, **facts: traces.append((args, facts)))
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "reachjudge":
            row = context["candidates"][0]
            return {"items": [{"id": row["id"], "kind": "person", "name": "Jane Smith", "role": "Practice Owner", "company": "", "location": "UK",
                "quote": row["evidence_pages"][0]["text"], "url": row["url"], "affiliation": "", "fit": 90, "why": "Published clinic owner deciding on bookings.", "value_exchange": "Owns an independent UK veterinary practice and decides on its booking workflow."}]}
        return {"items": [{"id": 0, "body": "Hi Jane, I'd value your perspective on clinic bookings. Open to a 15-minute call? Reply with a time.",
            "connection_note": "Hi Jane, could we discuss clinic bookings? Reply with a time."}] if personalised else []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    hits = [{"kind": "person", "url": "https://www.linkedin.com/in/jane-smith", "title": "Jane Smith - Practice Owner",
        "snippet": "Jane Smith is a Practice Owner in the UK. I run an independent veterinary practice and decide on bookings."}]
    context = {"mode": "B2B", "audience": "independent veterinary clinics in the UK", "problem": "A tool for clinic booking follow-ups", "competitors": [], "offer": ""}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"])
    leads = asyncio.run(reachable.discover(None, hits, [], context, _leased_payload(journey_db)))
    assert leads, traces
    lead = leads[0]
    assert lead["draft_source"] == ("model" if personalised else "template")
    assert ("I'd value your perspective" if personalised else "I'm researching a tool for") in lead["draft"]
    if not personalised:
        assert all("I'm researching a tool for clinic booking follow-ups." in lead[field]
            for field in ("draft", "connection_note", "x_note"))


@pytest.mark.parametrize("problem,phrase", [
    ("A tool for teams to follow up with teams.", "a tool for teams to follow up with teams"),
    ("An assistant for the clinic and its staff!", "an assistant for the clinic and its staff"),
    ("The workflow for reviewing drawings?", "the workflow for reviewing drawings"),
    ("Review structural drawings", "this workflow: Review structural drawings"),
    ("AI drawing review", "this workflow: AI drawing review"),
    ("", "this workflow"),
])
def test_call_templates_preserve_complete_phrases(problem, phrase):
    from app.gtm.reachable import _call_draft
    lead = {"name": "Jane Smith"}
    _call_draft(lead, problem)
    assert all(f"I'm researching {phrase}." in lead[field] for field in ("draft", "connection_note", "x_note"))
    assert all("15-minute call" in lead[field] for field in ("draft", "connection_note", "x_note"))


def test_long_call_topic_uses_complete_safe_template_instead_of_cutting_words():
    from app.gtm.reachable import _call_draft
    lead = {"name": "Jane Smith"}
    _call_draft(lead, "A tool for " + "careful appointment follow-ups " * 20)
    assert "I'm researching this workflow" in lead["draft"]
    assert "careful appointment follow-ups" not in lead["draft"]
    assert all(lead[field].endswith("Reply with a time.") for field in ("draft", "connection_note", "x_note"))
    assert len(lead["connection_note"]) <= 280 and lead["x_note"] == lead["connection_note"]


def test_u42_value_exchange_speaks_to_you():
    from app.gtm.discovery import Judgement
    item = Judgement(id=0, kind="business", affiliation="Independent school", url="https://taras.example", name="Tara's School for Dogs", quote="Training puppies",
        value_exchange="A research call could help the founder learn about puppy training.")
    assert item.value_exchange == "A research call could help you learn about puppy training."


def test_u43_draft_postprocessing_normalises_greeting_and_closing():
    from app.gtm.discovery import ReachDraft
    from app.gtm.reachable import normalise_draft
    lead = {"name": "Tara's School for Dogs", "company": "Tara's School for Dogs", "kind": "business"}
    raw = "Hi Tara's School for Dogs team, Your emphasis on puppy training is useful. Open to a call? reply with a time"
    checked = ReachDraft(id=0, body=raw, connection_note=raw)
    for note in (checked.body, checked.connection_note, normalise_draft(raw, lead)):
        assert note.startswith("Hi Tara's School for Dogs team,\n\nYour emphasis")
        assert note.endswith("? Reply with a time.")
    assert normalise_draft(normalise_draft(raw, lead), lead) == normalise_draft(raw, lead)


def test_u45_named_people_are_not_greeted_as_teams():
    from app.gtm.reachable import _call_draft, normalise_draft, recipient_name
    person = {"name": "Tonya Wilhelm", "company": "Global Dog Training", "kind": "person"}
    assert recipient_name(person) == "Tonya"
    assert normalise_draft("Hi Tonya Wilhelm team, Your work is useful. reply with a time", person).startswith("Hi Tonya,\n\n")
    _call_draft(person, "Puppy training")
    assert person["draft"].startswith("Hi Tonya,\n\n")
    assert recipient_name({"name": "Tara's School for Dogs", "company": "Tara's School for Dogs", "person": "Tara's School for Dogs"}).endswith(" team")


def test_progress_summary_never_exposes_search_operators():
    from app.gtm.journey import _discovery_summary
    plan = [{"intent": "business", "query": "independent veterinary clinics Bristol"},
        {"intent": "person", "query": 'site:linkedin.com/in intitle:"practice manager" -jobs -hiring -recruiter'},
        {"intent": "person", "query": 'site:x.com veterinary UK'},
        {"intent": "community", "query": 'site:reddit.com/r/veterinary bookings'}]
    summary = _discovery_summary("independent veterinary clinics in Bristol, Edinburgh, Manchester in the UK", plan)
    assert all(word in summary for word in ("Bristol", "Edinburgh", "Manchester", "LinkedIn", "X profiles", "Reddit r/veterinary"))
    assert not any(word in summary for word in ("site:", "intitle:", "-jobs", "-hiring", "-recruiter"))
    assert _discovery_summary("UK clinics", ["site:linkedin.com/in veterinary -jobs"]) == "Searching LinkedIn profiles."

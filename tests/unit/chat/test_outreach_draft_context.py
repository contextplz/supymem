"""Outreach drafts are grounded in the venture's brief, signed by the founder, and reached via the confirm card."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture as venture_fixture

load_company_os_app()


@pytest.fixture
def venture(venture_fixture, monkeypatch):
    from app.core.local_mode import is_local
    from tests.idea_fixtures import install_provider

    if is_local():
        with monkeypatch.context() as provider_patch:
            install_provider(provider_patch)
    return venture_fixture


def _sender_address(monkeypatch, address):
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("smtp", {"postal_address": address}, {})
    else:
        monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", address)


@pytest.mark.parametrize("venture_fixture,address", [
    ("Work order scheduling for factory maintenance teams", "14 Workshop Road, Boston, MA 02110"),
    ("Seasonal planting reminders for balcony gardeners", "28 Garden Street, Denver, CO 80202"),
], indirect=["venture_fixture"], ids=["b2b", "consumer"])
@pytest.mark.parametrize("sample", [False, True])
def test_local_sender_address_fixture_uses_saved_settings(venture_fixture, monkeypatch, address, sample):
    from app.core.local_secrets import read_settings
    from app.gtm.relationships.email_compliance import sender_address
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    if sample:
        assert venture_fixture[0].post("/local/providers/sample", json={}).status_code == 200
    _sender_address(monkeypatch, address)
    assert read_settings("smtp")["values"]["postal_address"] == address
    with db()() as session:
        assert sender_address(session, venture_fixture[2]) == address
        if sample:
            from app.gtm.local_mail import _smtp
            from app.gtm.supymem.composio_gmail import DefiniteFailure

            with pytest.raises(DefiniteFailure, match="Sample mode never sends email"):
                with _smtp(SimpleNamespace()):
                    pytest.fail("Your sample must not open an SMTP transport.")
            _sender_address(monkeypatch, "")
            assert sender_address(session, venture_fixture[2]) == "Sample sender address, not for sending"


@pytest.mark.parametrize("brand,pitch,subject,verb,object_words,mode", [
    ("Shelfwatch", "a tool for warehouse inventory checks", "their warehouse", "tracks", "missing pallets", "honest"),
    ("Mealmap", "an app for household meal planning", "they", "plan", "weekly meals", "honest"),
    ("Pipecheck", "a tool for municipal water inspections", "their utility", "finds", "pipe leaks", "honest"),
    ("Plantpal", "an app for balcony gardeners", "they", "manage", "watering schedules", "honest"),
    ("Shiftbook", "a tool for hotel staffing", "their hotel", "handles", "shift changes", "honest"),
    ("Readcircle", "an app for neighborhood book clubs", "they", "choose", "monthly books", "honest"),
    ("Labnote", "a tool for laboratory stock checks", "their laboratory", "tracks", "expired reagents", "long"),
    ("Tripnest", "an app for family travel planning", "they", "compare", "holiday routes", "long"),
    ("Freightdesk", "a tool for shipping dispatch teams", "their depot", "handles", "delayed deliveries", "long"),
    ("Runmate", "an app for recreational runners", "they", "find", "training partners", "long"),
    ("Caseclock", "a tool for legal scheduling", "their practice", "manages", "filing deadlines", "long"),
    ("Craftswap", "an app for local craft groups", "they", "organize", "material swaps", "long"),
    ("Roofnote", "a tool for roofing inspections", "their company", "finds", "roof damage", "experience"),
    ("Petpath", "an app for pet walking groups", "they", "plan", "walking routes", "proof"),
    ("Coldchain", "a tool for refrigerated transport", "their fleet", "tracks", "temperature changes", "live"),
])
def test_requested_draft_path_preserves_honest_models_and_specific_fallbacks(
        venture, monkeypatch, caplog, brand, pitch, subject, verb, object_words, mode):
    import json

    from app.autopilot import claims, outreach
    from app.chat import assistant, customers, offers
    from app.connections import mailboxes
    from app.credits import metered
    from app.db.models import GtmContact, Venture, Workspace
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.planning import execute
    from app.state import db
    from app.ventures import state_brief
    from core.clock import utcnow

    _, vid, wid = venture
    _sender_address(monkeypatch, "24 Research Lane, Boston, MA 02110")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: pitch)
    monkeypatch.setattr(outreach, "founder_signature", lambda _vid: "Morgan")
    monkeypatch.setattr(outreach, "venture_description", lambda _vid: pitch)
    monkeypatch.setattr(mailboxes, "enabled", lambda: False)
    monkeypatch.setattr(mailboxes, "resolve_send_mailbox", lambda *_args, **_kwargs: {
        "email": "morgan@sender.example", "connected_account_id": "ca_draft", "composio_user_id": "draft-founder",
        "mailbox_id": "mail_draft", "workspace_id": wid})
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": brand, "offer": pitch, "raw_idea": pitch, "problem": object_words}})
    async def checked_domains(_domains):
        return {"reader.example": "mx"}
    monkeypatch.setattr("app.intake.contacts.check_domains", checked_domains)
    with db()() as session:
        actor = session.get(Workspace, wid).owner_id
        session.get(Venture, vid).name = brand
        session.add(GtmContact(id="draft-reader", workspace_id=wid, first_name="Alex", email="alex@reader.example",
            source=f"venture:{vid}:research", enrichment_json=json.dumps({"contact_source_url": "https://reader.example/contact"}),
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    request = f"Draft an email to Alex at alex@reader.example asking how {subject} {verb} {object_words} today. Do not send."
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"user": "morgan@sender.example", "sender_name": "Morgan"}, {})
    question = f"How {'do you' if subject == 'they' else 'does ' + subject.replace('their', 'your')} {verb if subject == 'they' else verb[:-1]} {object_words} today?"
    opening = f"I'm building {brand}, {pitch}."
    honest = f"Subject: Your current approach\n\nHi Alex,\n\n{opening} Maybe a simpler approach could be useful, but I do not yet know what you need.\n\n{question}\n\nThanks,\nMorgan"
    extra = (" I would like to understand the process you already use before deciding what to build. "
             "This is an early idea, not a finished product or a sales pitch. A brief reply in your own words is plenty. "
             "Please leave out any private details. There is no need to share records or screenshots. "
             "Your perspective would help me decide whether this direction is worth pursuing before I spend time on features. "
             "I do not want to assume that a new tool is what you actually need.")
    model_body = honest if mode == "honest" else honest.replace(" Maybe", extra + " Maybe")
    if mode == "long":
        model_body = model_body.replace("Maybe a simpler approach could be useful, but I do not yet know what you need.", "")
        model_body = model_body.replace("\n\n" + question, " " + question)
    invented = {"experience": "I've interviewed operators about this.", "proof": "We have 500 paying customers.",
                "live": "Our platform automatically detects faults."}.get(mode)
    if invented:
        model_body = honest.replace(opening, opening + "\n\n" + invented)
    fallbacks, rewrites = [], []
    template, grounded = outreach._template, customers._grounded_draft
    def tracked_template(*args, **kwargs):
        fallbacks.append("template")
        return template(*args, **kwargs)
    def tracked_grounded(*args, **kwargs):
        fallbacks.append("grounded")
        return grounded(*args, **kwargs)
    monkeypatch.setattr(outreach, "_template", tracked_template)
    monkeypatch.setattr(customers, "_grounded_draft", tracked_grounded)
    async def complete(*_args, **_kwargs):
        return model_body
    async def rewrite(messages, **_kwargs):
        rewrites.append(messages)
        content = f"{opening} {question}" if "at most" in messages[0]["content"] else model_body.split("Hi Alex,\n\n", 1)[1]
        if invented:
            content = f"{opening} {invented} {question}"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(metered, "llm", rewrite)
    text = asyncio.run(execute._review_drafts(vid, "Draft one outreach email for me to review", "Founder request: " + request))
    with customers._as(vid, actor):
        saved = asyncio.run(offers._save_requested_email(vid, request, text, "requested-draft"))
    assert saved and not saved.get("blocked_reason")
    body = saved["body"]
    assert not claims.violations(body, founder_words=claims.founder_words(vid))
    journey.validate_draft_copy(body, venture_id=vid, generated=True)
    assert "Your email address is listed publicly on reader.example." in body
    from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT, unsubscribe_url
    link = unsubscribe_url(wid, "draft-reader", "alex@reader.example")
    assert OPT_OUT in body and ("Stop these emails: " + link if link else REPLY_OPT_OUT) in body and "24 Research Lane" in body
    assert "morgan@sender.example" in body and "Morgan" in body
    if invented:
        assert fallbacks and invented not in body
        fallback_question = "Could you tell me how " + subject.replace("their", "your").replace("they", "you") + f" {verb} {object_words} today?"
        assert fallback_question in body and brand in body and pitch in body
        assert body.index(brand) < body.index("From:")
        with customers._as(vid, actor):
            repaired = asyncio.run(customers._shorter(vid, wid, saved | {"body": body.replace("\n\nThanks,", f"\n\n{invented}\n\nThanks,", 1)}, request))
        assert "grounded" in fallbacks and fallback_question in repaired and brand in repaired and pitch in repaired
        assert not claims.violations(repaired, founder_words=claims.founder_words(vid))
        assert all(line in repaired for line in body.split("\n\n") if line.startswith(("Your email", "Stop these emails:", "Business outreach", "From:")))
        assert OPT_OUT in repaired and repaired.index("Morgan") < repaired.index("From:")
    else:
        assert not fallbacks, (fallbacks, caplog.text)
        assert question in body and opening in body
    assert body.index("\nMorgan") < body.index("From:")
    assert len(body.split()) <= 120


@pytest.mark.parametrize("pitch,problem", [
    ("a tool for hospital purchasing teams", "missing supplier invoices"),
    ("an app for birdwatchers", "arranging weekend outings"),
])
def test_grounded_email_fallback_does_not_claim_charlie_started_work(venture, monkeypatch, pitch, problem):
    from app.autopilot import claims, outreach
    from app.chat import assistant
    from app.chat import claims as chat_claims
    from app.planning import execute
    from app.ventures import state_brief

    _, venture_id, _ = venture
    monkeypatch.setattr(outreach, "saved_pitch", lambda _venture_id: pitch)
    monkeypatch.setattr(outreach, "founder_signature", lambda _venture_id: "Taylor")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"offer": pitch, "raw_idea": pitch, "problem": problem}})

    async def no_model(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(assistant, "_complete", no_model)
    request = f"Draft an email to reader@example.test about {problem}. Do not send."
    text = asyncio.run(execute._review_drafts(venture_id, "Draft one outreach email for me to review", "Founder request: " + request))
    assert text and pitch in text and problem.casefold() in text.casefold()
    assert not chat_claims.claims_action(text)
    assert not claims.violations(text, founder_words=claims.founder_words(venture_id))


@pytest.mark.parametrize("pitch,problem,recipient", [
    ("a tool for restaurant stock ordering", "missing ingredient deliveries", "Jordan Quinn"),
    ("an app for local swimming partners", "finding swimming companions", "Casey Brooks"),
])
@pytest.mark.parametrize("mode", ["honest", "retry", "fallback"])
def test_requested_email_claims_and_grounded_recovery(venture, monkeypatch, pitch, problem, recipient, mode):
    from app.autopilot import claims, outreach
    from app.chat import assistant
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.planning import execute
    from app.ventures import state_brief

    _, vid, _ = venture
    _sender_address(monkeypatch, "10 Test Road, Boston, MA 02110")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: pitch)
    monkeypatch.setattr(outreach, "founder_signature", lambda _vid: "Taylor")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"offer": pitch, "raw_idea": pitch, "problem": problem}})
    request = f"Draft an email to {recipient} at reader@example.test asking how they handle {problem} today. Do not send."
    honest = f"Draft 1\nSubject: Your approach\n\nHi {recipient},\n\nI'm learning how you handle {problem} today. How does this work for you?\n\nThanks,\nTaylor"
    invented = honest.replace("I'm learning", "I've interviewed customers about")
    attempts = []

    async def complete(*_args, **_kwargs):
        attempts.append(True)
        return honest if mode == "honest" or mode == "retry" and len(attempts) == 2 else invented

    monkeypatch.setattr(assistant, "_complete", complete)
    result = asyncio.run(execute._review_drafts(vid, "Draft one outreach email for me to review", "Founder request: " + request))
    assert result and f"Hi {recipient}," in result and problem.casefold() in result.casefold()
    assert OPT_OUT in result and "10 Test Road" in result
    assert "I've interviewed" not in result
    assert not claims.violations(result, founder_words=claims.founder_words(vid))
    assert len(attempts) == (1 if mode == "honest" else 2)


@pytest.mark.parametrize("problem", ["restaurant stock ordering", "finding swimming companions"])
@pytest.mark.parametrize("mode", ["honest", "unknown", "limit"])
def test_named_address_draft_routes_directly_with_saved_experiment(venture, monkeypatch, problem, mode):
    from app.autopilot import outreach
    from app.chat import assistant, customers, offers
    from app.core.local_mode import is_local
    from app.credits.errors import LIMIT_MESSAGE
    from app.experiments import recruitment
    from app.planning import store
    from app.ventures import state_brief
    from llm_gateway.errors import ProviderCallError

    client, vid, _ = venture
    monkeypatch.setattr(recruitment, "selected", lambda _vid: {"id": "saved-test"})
    monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: problem)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"offer": problem, "raw_idea": problem, "problem": problem}})
    async def complete(*_args, **_kwargs):
        if mode != "honest":
            raise ProviderCallError("insufficient_credits" if mode == "limit" else "provider_outcome_unknown", "Private provider details")
        return f"Draft 1\nSubject: Your approach\n\nHi Casey Brooks,\n\nHow do you handle {problem} today?\n\nThanks,\nTaylor"
    async def save(*_args, **_kwargs):
        return None
    def forbidden(*_args, **_kwargs):
        pytest.fail("A named-address draft must not become a Work task or confirmation")
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(offers, "_save_requested_email", save)
    monkeypatch.setattr(offers, "draft_prepare", forbidden)
    monkeypatch.setattr(store, "enqueue_task", forbidden)
    request = f"Draft an email to Casey Brooks at reader@example.test asking how they handle {problem} today. This is my own test address and I agreed to receive it."
    command = customers.command(vid, request)
    assert command and command[0] == "draft_address"
    batch = customers.command(vid, request.replace("an email", "2 emails"))
    assert not batch or batch[0] != "draft_address"
    response = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert response.status_code == 200, response.text
    reply = response.json()
    if mode != "honest":
        assert "Subject:" not in reply["body_md"] and "Private provider details" not in reply["body_md"]
        if mode == "limit":
            assert reply["body_md"] == LIMIT_MESSAGE
        elif is_local():
            assert "Settings > Keys and connections > Your AI provider" in reply["body_md"]
            assert "Nothing was sent" in reply["body_md"]
            assert any(action.get("retry") and action.get("action") == "status" for action in reply["actions"])
        else:
            assert "retry" in reply["body_md"].lower()
        return
    assert "Hi Casey Brooks," in reply["body_md"] and problem in reply["body_md"]
    assert "Nothing was sent" in reply["body_md"] and "what to change" in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "Open work" not in reply["body_md"] and "checks" not in reply["body_md"]


@pytest.mark.parametrize("pitch,problem,recipient", [
    ("a platform for warehouse inventory reconciliation", "inventory discrepancies", "Morgan Lee"),
    ("an app for coordinating neighborhood walks", "finding walking companions", "Avery Kim"),
])
@pytest.mark.parametrize("mode", ["natural", "retry", "unusable", "unknown", "limit"])
def test_requested_email_accepts_natural_copy_without_losing_safety(
        venture, monkeypatch, caplog, pitch, problem, recipient, mode):
    from app.autopilot import outreach
    from app.chat import assistant, offers
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.planning import execute
    from app.state import db
    from app.ventures import state_brief
    from core.clock import utcnow
    from llm_gateway.errors import ProviderCallError

    _, vid, wid = venture
    _sender_address(monkeypatch, "10 Test Road, Boston, MA 02110")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: pitch)
    monkeypatch.setattr(outreach, "founder_signature", lambda _vid: "Taylor")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"offer": pitch, "raw_idea": pitch, "problem": problem}})
    email = "recipient@example.test"
    with db()() as session:
        session.add(GtmContact(id="review-recipient", workspace_id=wid, first_name=recipient, email=email,
            source=f"venture:{vid}:founder", enrichment_json='{"founder_import":true}',
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    request = f"Draft an email to {recipient} at {email} asking for a 15 minute call about {problem}. Do not send."
    valid = (f"Subject: A short conversation about {problem}\n\nHi {recipient},\n\n"
             f"I'm exploring ways to help with {problem}. Would you have 15 minutes to tell me "
             "about your experience and how you handle this today?\n\nThanks,\nTaylor")
    attempts = []

    async def complete(messages, **kwargs):
        attempts.append((messages, kwargs))
        if mode == "unknown":
            raise ProviderCallError("provider_outcome_unknown", "Private provider details")
        if mode == "limit" and len(attempts) == 2:
            raise ProviderCallError("insufficient_credits", "Private balance details")
        return valid if mode == "natural" or mode == "retry" and len(attempts) == 2 else valid.replace("Subject:", "Title:")

    async def no_send(*args, **kwargs):
        pytest.fail("Writing must not send or post")

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(execute, "_run_social_post", no_send)
    reply = asyncio.run(offers.draft_run(vid, request=request))
    text = reply["body_md"]
    if mode in {"natural", "retry", "unusable"}:
        assert f"Hi {recipient}," in text and OPT_OUT in text and "10 Test Road" in text
        assert "Draft 1\nSubject:" in text
        assert "I have your address from my own contacts." in text
        if mode != "unusable":
            assert "I have your address from my own contacts." in outreach.review_notes(vid)[0]["text"]
        assert len(attempts) == (1 if mode == "natural" else 2)
        assert all("without recipient names" not in str(messages) for messages, _ in attempts)
    else:
        assert "Here it is" not in text and "Here are the drafts" not in text and "Draft only" not in text
        assert "Nothing was sent" in text or "nothing was sent" in text
        assert "try again" in text.lower() or "settings" in text.lower() or "open" in text.lower()
        assert len(attempts) == (1 if mode == "unknown" else 2)
    assert recipient not in caplog.text and "Private provider details" not in text
    if mode in {"retry", "unusable"}:
        assert "email_format" in caplog.text
    if mode == "retry":
        assert attempts[0][1]["request_id"] != attempts[1][1]["request_id"]
    if mode == "limit":
        assert "Buy more in Settings" in text and "Private balance details" not in text
    context = {"pitch": pitch, "problem": problem, "audience": "", "founder": "Taylor", "link": "", "request": request}
    assert not execute.drafts_on_topic(valid.replace(recipient, "Unknown Recipient"), context)
    assert not execute.drafts_on_topic(valid.replace(recipient, email.split("@", 1)[0]), context)
    assert not execute.drafts_on_topic(valid.replace(problem, "spacecraft thermal simulations"), context)


@pytest.mark.parametrize("text", [
    "Make a landing page that collects emails for early access",
    "Write a blog post about cold emails for founders",
    "Prepare a plan for sending interview invitations to dentists",
])
def test_work_that_merely_mentions_email_is_not_an_email_request(text):
    from app.chat.intent import outreach_email_request

    assert not outreach_email_request(text)


@pytest.mark.parametrize("audience,pitch,count,words,purpose", [
    ("hotel maintenance supervisors", "a tool that tracks repair handoffs", 1, 120,
     "follow-up email to a current pilot supervisor. Ask for a measurable acceptance criterion, "
     "the risk review owner and purchasing contact before discussing a paid agreement"),
    ("home bakers", "an app that organizes bread recipes", 2, 75,
     "interview invitations about how they record recipe changes. Make no claims that you have "
     "customers or a working product"),
])
def test_chat_email_request_preserves_purpose_count_length_and_footer(
        venture, monkeypatch, audience, pitch, count, words, purpose):
    from app.chat import assistant
    from app.chat.intent import classify_intent, split_speech_acts
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.planning import execute
    from app.ventures import state_brief

    client, vid, _ = venture
    request = f"That's the only change. Draft {count} {words}-word {purpose}. Do not send it."
    if count > 1:
        request += "\nBackground: " + "You have no evidence about their current workflow. " * 30
        request += "\nDo not imply anyone has already agreed to participate."
    assert classify_intent(request) == "outreach_draft"
    assert split_speech_acts(request) == [("outreach_draft", request)]
    _sender_address(monkeypatch, "10 Test Road, Boston, MA 02110")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: {
        "idea": {"name": "Your project", "audience": audience, "offer": pitch, "raw_idea": pitch}})
    prompts = []
    body = f"I'm writing about {pitch}. {purpose.split('. ', 1)[1]}. "
    body += "Please share your perspective on this specific request. " * words
    body = "Hi,\n\n" + " ".join(body.split()[:words - 3]) + "\n\nThanks,\nYou"
    model_text = "\n\n".join(f"Draft {number}\nSubject: Your perspective\n\n{body}"
                              for number in range(1, count + 1))

    async def complete(messages, **kwargs):
        assert kwargs.get("request_id")
        prompts.append(messages)
        return model_text

    async def forbidden(*_args, **_kwargs):
        pytest.fail("A chat draft must not send or post anything")

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(execute, "_run_social_post", forbidden)
    monkeypatch.setattr("app.autopilot.outreach.founder_signature", lambda _vid: "You")
    assert execute.drafts_on_topic(model_text, {"pitch": pitch, "problem": pitch,
        "audience": audience, "link": "", "founder": "You"}), model_text
    response = client.post(f"/ventures/{vid}/chat", json={"message": request})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert not any(action.get("confirmation") for action in reply["actions"])
    blocks = re.findall(r"```text\n([\s\S]*?)\n```", reply["body_md"])
    assert len(blocks) == count, reply["body_md"]
    for block in blocks:
        assert OPT_OUT in block and "10 Test Road, Boston, MA 02110" in block
        email = block.split("\n\n", 1)[1].split(OPT_OUT, 1)[0].strip()
        assert len(email.split()) == words
        assert purpose.split(". ", 1)[1] in block
    prompt = "\n".join(message["content"] for message in prompts[-1])
    assert request in prompt
    assert "under 90 words" not in prompt
    assert "Do not write compliance footers" in prompt
    assert "asking for a short conversation or early-access interest" not in prompt
    for invalid in ("", model_text.replace("Subject:", "Title:"), model_text + "\n\nDraft 9\nSubject: Extra\n\nHi,",
                    model_text.replace(body, " ".join(body.split()[:words // 2]), 1)):
        model_text = invalid
        fallback = asyncio.run(execute._review_drafts(vid, "Draft outreach emails for me to review",
            "Founder request: " + request))
        assert len(re.findall(r"(?im)^Draft \d+", fallback)) == count
        assert "Subject:" in fallback and OPT_OUT in fallback


def test_teammate_drafts_use_finished_idea_review_without_a_customer_search(venture, monkeypatch):
    import json

    from app.autopilot import outreach
    from app.chat import assistant
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from app.planning import execute
    from app.state import db
    from core.clock import utcnow

    client, _, _ = venture
    domains = [
        ("Dispatch Ledger", "warehouse dispatch teams", "a tool that groups delayed shipments by carrier",
         "Dispatch teams lose hours reconciling late shipments"),
        ("Trail Circle", "weekend hikers", "an app that matches hikers with nearby walking partners",
         "Hikers struggle to find companions for local weekend walks"),
    ]
    async def offline(*_args, **_kwargs):
        return ""
    monkeypatch.setattr(assistant, "_complete", offline)
    for name, audience, pitch, problem in domains:
        response = client.post("/ventures", json={"name": name, "raw_idea": pitch})
        assert response.status_code == 201, response.text
        vid, wid = response.json()["id"], response.json()["workspace_id"]
        now = utcnow()
        with db()() as session:
            review = session.query(IdeaSession).filter_by(venture_id=vid).first()
            if review is None:
                review = IdeaSession(id=f"idea_{vid}", workspace_id=wid, venture_id=vid, case="know",
                                     state="confirmed", created_at=now, updated_at=now)
                session.add(review)
                session.flush()
            revision = IdeaRevision(id=f"review_{vid}", session_id=review.id, workspace_id=wid, venture_id=vid,
                rev=10, source="agent", summary=pitch,
                structured=json.dumps({"customer": audience, "offer": pitch, "problem": problem}), created_at=now)
            session.add(revision)
            review.state = "confirmed"
            review.current_revision_id = review.confirmed_revision_id = revision.id
            session.commit()
            assert journey._row(session, vid) is None
        assert not outreach.saved_pitch(vid)
        text = asyncio.run(execute._review_drafts(vid, "Draft outreach emails for you to review", ""))
        bodies = re.split(r"Draft \d+\n", text)[1:]
        assert len(bodies) == 3 and audience in text
        for body in bodies:
            assert pitch.lower() in body.lower() and problem.lower() in body.lower()
            assert "a new product" not in body and "faced this problem" not in body
            assert "Customer:" not in body and "Offer:" not in body and "Problem:" not in body
            assert "—" not in body
        async def incomplete_model(*_args, **_kwargs):
            return "\n\n".join(f"Draft {number}\nSubject: Your experience\n\nHi,\n\nI'm exploring "
                               f"{pitch if number == 1 else 'a new product'}. Would you share your experience?"
                               for number in (1, 2, 3))
        monkeypatch.setattr(assistant, "_complete", incomplete_model)
        repaired = asyncio.run(execute._review_drafts(vid, "Draft outreach emails for you to review", ""))
        assert repaired.lower().count(pitch.lower()) == 3 and repaired.lower().count(problem.lower()) == 3
        assert "a new product" not in repaired
        monkeypatch.setattr(assistant, "_complete", offline)


def test_draft_cards_normalize_existing_fences_for_unrelated_domains():
    from app.chat.offers import format_drafts

    for topic in ("warehouse stock forecasting", "student music playlists"):
        drafts = [f"Draft {number}\nSubject: Your {topic}\n\nHi,\n\nCould you share your experience?"
                  for number in (1, 2, 3)]
        plain = "\n\n".join(drafts)
        individually_fenced = "\n\n".join(f"```text\n{draft}\n```" for draft in drafts)
        nested = "\n\n".join(f"```text\n```text\n{draft}\n```\n```" for draft in drafts)
        for source in (plain, individually_fenced, f"```text\n{plain}\n```", nested,
                       individually_fenced.rsplit("\n```", 1)[0], plain.replace("Draft 3", "### Draft 3")):
            formatted = format_drafts(source)
            blocks = re.findall(r"(?m)^```text\n([\s\S]*?)\n```(?=\n|$)", formatted)
            assert len(blocks) == 3
            assert all(f"Draft {number}" in blocks[number - 1] for number in (1, 2, 3))
            assert all("```" not in block and not re.search(r"(?m)^text$", block) for block in blocks)
            assert format_drafts(formatted) == formatted


def test_review_drafts_restore_saved_product_capitalization_across_domains(venture, monkeypatch):
    from app.autopilot import outreach
    from app.chat import assistant
    from app.credits import metered
    from app.gtm import journey, playbooks
    from app.planning import execute
    from app.state import db
    from app.ventures import state_brief

    client, _, _ = venture
    for name, audience, product, pitch in [
        ("QueueLens", "warehouse managers", "Jira", "a queue tracker for warehouse managers using jira"),
        ("CampusMix", "college students", "Spotify", "a playlist planner for college students using spotify"),
    ]:
        raw = f"{pitch}. You already use {product} and API tools."
        created = client.post("/ventures", json={"name": name, "raw_idea": raw}).json()
        vid = created["id"]
        with db()() as session:
            journey._store(session, journey._row(session, vid, create=True), {"pitch": pitch})
            session.commit()
        clean = pitch.replace(product.lower(), product)
        assert outreach.idea_phrase(raw) == clean
        assert outreach.saved_pitch(vid) == clean
        async def generate_pitch(*args, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=pitch))])
        monkeypatch.setattr(metered, "llm", generate_pitch)
        assert asyncio.run(playbooks.pitch(created["workspace_id"], vid, raw)) == clean
        assert outreach.preserve_capitalization(f"{product.lower()} https://example.test/{product.lower()} hello@{product.lower()}.example",
                                                raw) == f"{product} https://example.test/{product.lower()} hello@{product.lower()}.example"
        brief = {"idea": {"name": name, "raw_idea": raw, "audience": audience, "problem": pitch}}
        monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
        calls = []
        async def complete(messages, **kwargs):
            assert clean in messages[-1]["content"]
            calls.append(messages)
            return "\n\n".join(f"Draft {number}\nSubject: Your workflow\n\nHi,\n\nI'm exploring {pitch}. "
                               "Would you share your experience?" for number in (1, 2, 3))
        monkeypatch.setattr(assistant, "_complete", complete)
        modeled = asyncio.run(execute._review_drafts(vid, "Draft outreach emails for you to review", ""))
        assert calls and modeled.count(f"I'm exploring {clean}.") == 3
        async def offline(*args, **kwargs):
            return ""
        monkeypatch.setattr(assistant, "_complete", offline)
        fallback = asyncio.run(execute._review_drafts(vid, "Draft outreach emails for you to review", ""))
        assert clean in fallback and product.lower() not in fallback

URL = "https://fixture-fitness.supymem.com/"
BRIEF = {
    "idea": {"name": "ClubCheck", "audience": "independent gym owners",
             "raw_idea": "software that flags gym members likely to cancel from check-in data and prompts the "
                         "owner to send a personal check-in"},
    "website": {"url": URL, "waitlist_url": URL + "#waitlist"},
    "research": {"summary": "Members who stop visiting tend to cancel within weeks."},
    "experiments": ["- proposed: ask one owner to try a manual at-risk member alert"],
}
OFF_TOPIC = ("Draft 1\nSubject: Idea for unused gym space\n\nHi [First name],\n\nGyms often have valuable floor "
             "space tied up in rarely-used machines.\n\nThanks,\n[Founder name]")
GOOD = ("Draft 1\nSubject: Members about to cancel\n\nHi [First name],\n\nClubCheck flags gym members likely "
        f"to cancel from check-in data. Join the waitlist: {URL}#waitlist\n\nThanks,\n[Founder name]")

CARE_INTERVIEW = (
    "Draft a short email inviting an adult childcare provider to a voluntary non-sales discovery interview for Provider Planner. "
    "The public business website https://little-sprouts.example.com/ identifies Quinn Hart as the operator of Little Sprouts Care "
    "and publishes hello@little-sprouts.example.com; licensed/home-provider size fit is unknown and must not be claimed. "
    "We have no product, interviews, users or validated pain. Request only a 10–15 minute call or email reply "
    "about the provider’s own general administrative workflow and what was last frustrating. No observation "
    "of childcare or pickup, no parent conversations, no child names, photos, attendance or health records, "
    "family contacts, private messages or screenshots. Give the exact subject and message I can review and "
    "send from our spare Gmail. Do not send anything or search for other contacts.")


def test_single_interview_email_preserves_exact_constraints_and_review_only(venture):
    from app.chat.guard import _bulk_outreach
    from app.chat.intent import classify_intent, split_speech_acts
    from app.db.models import PlannedTask
    from app.state import db
    from sqlalchemy import select

    client, vid, _ = venture
    assert not _bulk_outreach(CARE_INTERVIEW)
    assert classify_intent(CARE_INTERVIEW) == "outreach_draft"
    assert split_speech_acts(CARE_INTERVIEW) == [("outreach_draft", CARE_INTERVIEW)]
    with db()() as session:
        before = list(session.scalars(select(PlannedTask.id).where(PlannedTask.venture_id == vid)))
    reply = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft",
        "payload": {"run": False, "recruitment_request": CARE_INTERVIEW}}).json()
    assert reply["kind"] == "confirmation"
    card = next(a["confirmation"] for a in reply["actions"] if a.get("confirmation"))
    assert card["resulting_state"] == {"run": True, "recruitment_request": CARE_INTERVIEW}
    assert "one interview email" in card["interpretation"]
    assert next(a["label"] for a in reply["actions"] if a.get("confirmation")) == "Draft the email"
    with db()() as session:
        assert list(session.scalars(select(PlannedTask.id).where(PlannedTask.venture_id == vid))) == before


@pytest.mark.parametrize("text", [
    "Draft an interview email and then send it to the provider.",
    "Draft a short email for an interview, send it now.",
    "Draft a short email for an interview: please send it now.",
    "Draft a discovery email. Send it now.",
    "Send an interview email to contact@consumer.example.",
    "Should I draft an interview email?",
    "Do not draft an interview email.",
    "Draft an interview email to one@example.com and two@example.com.",
])
def test_single_interview_email_does_not_consume_send_question_or_multiple_targets(text):
    from app.chat.intent import single_interview_email_request
    assert not single_interview_email_request(text)


def test_duration_does_not_hide_actual_bulk_recipient_count():
    from app.chat.guard import _bulk_outreach
    assert _bulk_outreach("Email 15 founders about a 10–15 minute call or email reply")
    assert _bulk_outreach("Send the 15-minute interview invitation to all contacts")


def test_eligible_interview_prefixes_preserve_no_send_review():
    from app.chat.intent import single_interview_email_request
    assert single_interview_email_request("Draft an interview email for my review. Do not send it.")
    assert single_interview_email_request("Draft a short email for an interview. Do not send anything.")


@pytest.mark.parametrize("model_text", ["", "Draft 1\nSubject: Administrative workflow\n\nHi [First name],\n"
    "I'm researching an unvalidated idea about administrative workflow for home childcare providers. We have no product. Could we discuss your own "
    "general administrative workflow? No client or child details, records or private messages needed.\nThanks,\nSamira"])
def test_one_interview_draft_is_durable_without_delivery_or_signup_pitch(venture, monkeypatch, model_text):
    from app.chat import assistant
    from app.planning import execute
    from app.ventures import state_brief
    from app.db.models import PlannedTask, VentureDocument
    from app.state import db
    from sqlalchemy import select

    prompts = []
    async def complete(messages, **_kw):
        prompts.append(messages)
        return model_text
    async def forbidden(*_a, **_kw):
        pytest.fail("reviewed interview draft must not deliver externally")
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(execute, "_run_social_post", forbidden)
    monkeypatch.setattr("app.autopilot.outreach.founder_signature", lambda _v: "Samira")
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        **BRIEF, "idea": {"name": "Provider Planner", "audience": "home childcare providers",
            "raw_idea": "An unvalidated idea about administrative workflow for home childcare providers."}})
    client, vid, _ = venture
    done = client.post(f"/ventures/{vid}/chat", json={"message": CARE_INTERVIEW})
    assert done.status_code == 200, done.text
    body = done.json()["body_md"]
    assert "Nothing was sent" in body and URL not in body
    assert any(CARE_INTERVIEW in m["content"] for m in prompts[-1])
    with db()() as session:
        task = session.scalars(select(PlannedTask).where(PlannedTask.venture_id == vid,
            PlannedTask.title == "Draft one interview email for me to review")).one()
        assert "one interview email" in task.detail and CARE_INTERVIEW in task.detail
        documents = list(session.scalars(select(VentureDocument).where(VentureDocument.venture_id == vid)))
        assert "Subject:" in body and "Draft 1" in body and "Draft 2" not in body
        assert "no product" in body.lower() and task.status == "done"
        assert any("Subject:" in (doc.body_md or "") for doc in documents)


def run(monkeypatch, venture, llm_text):
    _, vid, _ = venture
    from app.chat import assistant
    from app.planning import execute
    from app.ventures import state_brief

    async def fake(*_a, **_k):
        return llm_text

    monkeypatch.setattr(assistant, "_complete", fake)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_k: BRIEF)
    monkeypatch.setattr("app.autopilot.outreach.founder_signature", lambda _v: "Samira")
    return asyncio.run(execute._review_drafts(vid, "Draft outreach emails", ""))


@pytest.mark.parametrize("llm_text", [OFF_TOPIC, "", GOOD])
def test_drafts_are_on_topic_linked_and_signed(monkeypatch, venture, llm_text):
    out = run(monkeypatch, venture, llm_text)
    low = out.lower()
    assert "cancel" in low and URL in out
    assert "[Founder name]" not in out and "Samira" in out and "[First name]" not in out
    assert "unused" not in low


def test_template_uses_brand_live_url_and_signature_without_placeholders(monkeypatch, venture):
    from app.autopilot.outreach import founder_signature
    from app.firstlook import service

    brief = {**BRIEF, "idea": {**BRIEF["idea"], "name": "Club Attendance"},
             "website": {"url": URL, "live": True, "waitlist_url": "https://gateway.example/public/waitlists/ven_1"}}
    monkeypatch.setattr(service, "plan_brief", lambda _v: {"name": "ClubCheck"})
    _, vid, _ = venture
    from app.chat import assistant
    from app.planning import execute
    from app.ventures import state_brief

    async def fake(*_a, **_k):
        return ""

    monkeypatch.setattr(assistant, "_complete", fake)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_k: brief)
    out = asyncio.run(execute._review_drafts(vid, "Draft outreach emails", ""))
    assert "ClubCheck" in out and "Club Attendance" not in out
    assert URL in out and "waitlists" not in out
    assert founder_signature(vid) in out and "Founder" not in out


@pytest.mark.parametrize("pitch_source", ["saved", "generated", "offline"])
@pytest.mark.parametrize("model_result", ["clean", "notes", "unsafe", "empty"])
def test_review_builders_use_clean_venture_copy_for_models_and_fallbacks(venture, monkeypatch, pitch_source, model_result):
    from app.autopilot import outreach
    from app.chat import assistant
    from app.credits import metered
    from app.firstlook import service
    from app.planning import execute
    from app.ventures import state_brief

    client, _, _ = venture
    cases = [
        ("StitchBox", "home sewists in Uruguay", "a pattern fitting guide for home sewists",
         "Patterns do not fit the fabric they have"),
        ("AquiferOps", "wastewater plant coordinators in Denmark", "a maintenance handover tool for wastewater plants",
         "Maintenance handovers lose important equipment details"),
    ]
    monkeypatch.setenv("LLM_OFFLINE", "1" if pitch_source == "offline" else "0")
    monkeypatch.setattr(outreach, "founder_signature", lambda _vid: "Samira")
    monkeypatch.setattr(service, "plan_brief", lambda _vid: {})
    pitches, template_calls = [], []
    template = execute._template_drafts

    def fallback(ctx):
        template_calls.append(ctx)
        return template(ctx)

    async def compose_pitch(messages, **kwargs):
        assert kwargs["purpose"] == "venture.chat.assistant" and kwargs["workspace_id"]
        pitches.append(kwargs["venture_id"])
        assert messages[-1]["content"] == raw
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=pitch))])

    monkeypatch.setattr(metered, "llm", compose_pitch)
    monkeypatch.setattr(execute, "_template_drafts", fallback)
    for name, audience, pitch, problem in cases:
        created = client.post("/ventures", json={"name": name, "raw_idea": pitch})
        assert created.status_code == 201, created.text
        vid = created.json()["id"]
        raw_phrase = pitch if pitch_source == "offline" else f"{name} is an early idea for {audience} to address {problem.lower()}"
        # A saved pitch is reused; otherwise the founder's text is cleaned (one paid call per review task, no pitch call).
        expected_pitch = (pitch if pitch_source in {"saved", "offline"} else
            f"an early idea for {audience} to address {problem.lower()}")
        raw = (f"Product: something for {raw_phrase} idk (rough draft). "
               f"Customer: i guess mostly {audience}, still messy. Problem: maybe {problem.lower()}.")
        url = f"https://{name.lower()}.example/"
        brief = {
            "idea": {"name": name, "raw_idea": raw,
                     "audience": f"Audience: i guess mostly {audience}, idk, still messy; " + "unreviewed qualifier " * 20,
                     "problem": f"Problem: i think maybe {problem.lower()}, idk, rough notes"},
            "website": {"live": True, "url": url, "waitlist_url": "https://foreign.example/waitlist"},
            "research": {"summary": raw},
            "experiments": ["- proposed: At least one adult is already spending money, time or effort on a regional-s"],
            "gtm": {"bottleneck": "proposed: Unreviewed hypothesis must stay internal"},
        }
        monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_args, **_kwargs: brief)
        monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: "I'm building " + pitch if pitch_source == "saved" else "")
        for title in ("Draft outreach emails for you to review", "Draft one interview email for me to review",
                      "Draft recruitment posts for me to review"):
            async def complete(messages, **kwargs):
                assert kwargs["workspace_id"] == created.json()["workspace_id"]
                context = messages[-1]["content"]
                assert expected_pitch in context and audience in context and problem in context
                assert raw not in context and "regional-s" not in context and "Unreviewed hypothesis" not in context
                assert not re.search(r"\b(?:idk|i guess|mostly|rough notes|still messy|proposed:)\b", context, re.I)
                if model_result == "empty":
                    return ""
                count = 3 if "outreach emails" in title else 1
                is_post = "posts" in title
                body = (f"I'm exploring {name}: {expected_pitch}. {problem}. Would you share what you tried?")
                if "interview" not in title:
                    body += f" Learn more: {url}"
                if model_result == "notes":
                    body += " Customer: i guess mostly " + audience + ". proposed: At least one regional-s"
                if model_result == "unsafe":
                    body = body[:1].lower() + body[1:] if is_post else body + " Visit https://invented.example/."
                greeting = "Hi Morgan," if model_result == "unsafe" and "interview" in title else "Hi [First name],"
                return "\n\n".join(f"Draft {number}\n" + ("" if is_post else f"Subject: A question about {name}\n\n{greeting}\n\n")
                                   + body + "\n\nThanks,\n[Founder name]" for number in range(1, count + 1))

            monkeypatch.setattr(assistant, "_complete", complete)
            before = len(template_calls)
            text = asyncio.run(execute._review_drafts(vid, title, "Do not send anything."))
            assert expected_pitch in text and name in text and problem in text and raw not in text
            assert not re.search(r"\[[^\]]*\]|\b(?:idk|i guess|mostly|rough notes|still messy|Customer:|Audience:|Problem:|proposed:|My idea:)\b", text, re.I)
            assert "regional-s" not in text and "At least one" not in text and "—" not in text
            assert "foreign.example" not in text and "invented.example" not in text
            assert "Morgan" not in text
            assert not re.search(r"[.!?][ \t]+[a-z]", text)
            assert ("Would you share what you tried?" in text) == (model_result == "clean")
            if "outreach emails" in title:
                assert len(template_calls) == before + (model_result != "clean")
                assert text.count("Draft ") == 3 and url in text
            if "interview" in title:
                assert url not in text and "Subject:" in text and "Hi," in text
    assert not pitches  # the review task's single paid call is the draft itself


def test_founder_first_name_reads_greeting_desk_setting(venture):
    _, vid, _ = venture
    from app.api.greeting_api import _update
    from app.db.models import Venture, Workspace
    from app.planning import execute
    from app.state import db

    with db()() as session:
        owner = session.get(Workspace, session.get(Venture, vid).workspace_id).owner_id
    assert execute._founder_first_name(vid) is None  # "Founder" placeholder is never a name
    _update(owner, first_name="Samira")
    assert execute._founder_first_name(vid) == "Samira"


def test_review_draft_request_opens_confirm_card_not_task(venture):
    client, vid, _ = venture
    from app.chat.intent import classify_intent

    text = "draft outreach emails to gym owners for me to review"
    assert classify_intent(text) == "outreach_draft"
    reply = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft",
        "payload": {"run": False, "recruitment_request": text}}).json()
    assert reply["kind"] == "confirmation"
    assert any(a.get("confirmation") for a in reply["actions"])


@pytest.mark.parametrize("text", [
    "Based on the saved GTM diagnosis, draft a recruitment post for review. Do not post it.",
    "Using the saved GTM diagnosis draft three recruitment posts for Reddit.",
    "Please write a pilot recruitment post for LinkedIn.",
    "The saved GTM diagnosis recommends interviewing independent groomers. Please draft a short, honest "
    "recruitment post for a relevant community, with my live waitlist URL https://fixture-grooming.supymem.com/ "
    "and a clear request to learn about their last no-show. Specify the exact community or channel and where "
    "to click/paste. We have zero real requests and only a landing page, so do not claim customers or a working "
    "app. Use the current saved diagnosis and give me one concrete action I can execute now.",
])
def test_saved_gtm_composition_preserves_review_gate(venture, text):
    from app.chat.intent import classify_intent
    from app.db.models import PlannedTask
    from app.state import db
    from sqlalchemy import select

    client, vid, _ = venture
    assert classify_intent(text) == "outreach_draft"
    with db()() as session:
        before = list(session.scalars(select(PlannedTask.id).where(PlannedTask.venture_id == vid)))
    reply = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft",
        "payload": {"run": False, "recruitment_request": text}}).json()
    assert reply["kind"] == "confirmation"
    card = next(a["confirmation"] for a in reply["actions"] if a.get("confirmation"))
    assert card["action"] == "outreach.draft"
    assert card["resulting_state"] == {"run": True, "recruitment_request": text}
    assert "posts" in card["interpretation"]
    with db()() as session:
        assert list(session.scalars(select(PlannedTask.id).where(PlannedTask.venture_id == vid))) == before


@pytest.mark.parametrize("text", [
    "Do not draft recruitment posts; show the saved GTM diagnosis.",
    "Should I draft a recruitment post based on the GTM diagnosis?",
    "Show my recruitment posts.",
])
def test_recruitment_lookup_and_negation_are_not_write_requests(text):
    from app.chat.intent import recruitment_draft_request
    assert not recruitment_draft_request(text)


@pytest.mark.parametrize("model_result", ["", GOOD])
def test_reviewed_recruitment_draft_is_saved_without_external_delivery(venture, monkeypatch, model_result):
    from app.chat import assistant
    from app.planning import execute
    from app.ventures import state_brief
    from app.db.models import PlannedTask, VentureDocument
    from app.state import db
    from sqlalchemy import select

    prompts = []
    async def complete(messages, **_kw):
        prompts.append(messages)
        return model_result  # outage or email-shaped output still produces a grounded post
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: {
        **BRIEF, "gtm": {"present": True, "bottleneck": "No owner interviews", "now_items": ["Recruit three owners"]}})
    async def forbidden(*_a, **_kw):
        pytest.fail("draft-only work must never call social delivery")
    monkeypatch.setattr(execute, "_run_social_post", forbidden)
    client, vid, _ = venture
    from app.planning.store import enqueue_task
    stale = enqueue_task(vid, {"title": "Draft recruitment posts for me to review",
                               "detail": "Old request", "runnable": False})
    text = "Based on the saved GTM diagnosis, draft recruitment posts for me to review. Do not send anything."
    done = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert done.status_code == 200, done.text
    body = done.json()["body_md"]
    assert "Draft 1" in body and "ClubCheck" in body and "Subject:" not in body
    assert "Nothing was sent" in body and "Draft 2" not in body
    assert "Suggested channel" in body and "unverified" in body and "Start a post" in body
    assert any("No owner interviews" in m["content"] for m in prompts[-1])
    assert any("recruitment posts" in m["content"] for m in prompts[-1])
    with db()() as session:
        task = session.scalars(select(PlannedTask).where(
            PlannedTask.venture_id == vid, PlannedTask.title == "Draft recruitment posts for me to review",
            PlannedTask.status == "done")).one()
        assert task.id != stale["id"]
        assert task.status == "done"
        docs = list(session.scalars(select(VentureDocument).where(VentureDocument.venture_id == vid)))
        assert any("Draft 1" in (d.body_md or "") for d in docs)



def test_recruitment_fallback_preserves_no_show_ask_and_requested_count(venture, monkeypatch):
    from app.chat import assistant
    from app.planning import execute
    from app.ventures import state_brief
    async def unavailable(*_a, **_kw):
        return ""
    monkeypatch.setattr(assistant, "_complete", unavailable)
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *_a, **_kw: BRIEF)
    _, vid, _ = venture
    text = asyncio.run(execute._review_drafts(vid, "Draft recruitment posts for me to review",
        "Draft one recruitment post for Reddit to learn about their last no-show. Do not post anything."))
    assert "last no-show" in text and "Draft 2" not in text
    assert "r/smallbusiness" in text and "unverified" in text and "Create Post" in text
    multiple = asyncio.run(execute._review_drafts(vid, "Draft recruitment posts for me to review",
        "Draft three recruitment posts for review."))
    assert "Draft 3" in multiple and "Draft 4" not in multiple


CLEANER_INTERVIEW = (
    "Draft one short interview email for me to review for CleanCrew. Following your public-business "
    "Google-to-Gmail discovery procedure, I checked https://www.sample-cleaning.test/ directly. "
    "It describes house-cleaning services in Bangalore, a team of trained professionals and its own "
    "published business contact info@sample-cleaning.test. This is a cleaning company and adjacent prospect, "
    "not a verified solo cleaner. Manual/WhatsApp scheduling use, reschedule pain and willingness to pay are unknown. "
    "We have only an unvalidated concept, no product, interviews, users, payments or measured losses. "
    "Request a voluntary 10-15 minute non-sales call or brief email reply from the owner or scheduling manager "
    "about their own general booking/reschedule workflow and what was frustrating the last time a job changed "
    "at short notice. State we are learning before building and make no product-access promise. "
    "No client names, addresses, private chats, screenshots, calendars, contact lists or records. "
    "Give subject and body with a founder-name placeholder. Do not send, search, build or publish anything.")


def test_exact_cleaner_email_with_negative_lists_prepares_review_without_search(venture, monkeypatch):
    from app.chat.intent import classify_intent, single_interview_email_request, split_speech_acts
    from app.chat.search_recovery import search_forbidden
    from app.db.models import Job, PlannedTask
    from app.state import db
    from sqlalchemy import select
    client, vid, _ = venture
    def forbidden(*_args, **_kwargs):
        pytest.fail("review-only email preparation cannot search or return a manual lookup guide")
    monkeypatch.setattr('app.research.explore.explore_web', forbidden)
    monkeypatch.setattr('app.chat.search_recovery.manual_discovery_reply', forbidden)
    assert search_forbidden(CLEANER_INTERVIEW)
    assert single_interview_email_request(CLEANER_INTERVIEW)
    assert classify_intent(CLEANER_INTERVIEW) == 'outreach_draft'
    assert split_speech_acts(CLEANER_INTERVIEW) == [('outreach_draft', CLEANER_INTERVIEW)]
    with db()() as session:
        jobs = list(session.scalars(select(Job.id)))
        tasks = list(session.scalars(select(PlannedTask.id)))
    reply = client.post(f'/ventures/{vid}/chat/confirm', json={'action': 'outreach.draft',
        'payload': {'run': False, 'recruitment_request': CLEANER_INTERVIEW}}).json()
    assert reply['kind'] == 'confirmation'
    card = next(a['confirmation'] for a in reply['actions'] if a.get('confirmation'))
    assert card['resulting_state'] == {'run': True, 'recruitment_request': CLEANER_INTERVIEW}
    with db()() as session:
        assert list(session.scalars(select(Job.id))) == jobs
        assert list(session.scalars(select(PlannedTask.id))) == tasks


@pytest.mark.parametrize('suffix', [
    'Do not send, search, build or publish anything.',
    'Do not send, search.',
    "Don't publish, search or send anything.",
    'Never build, search and publish.',
])
def test_bare_verb_prohibition_lists_keep_email_review_and_forbid_search(suffix):
    from app.chat.intent import single_interview_email_request
    from app.chat.search_recovery import search_forbidden
    text = 'Draft one interview email for review. ' + suffix
    assert search_forbidden(text)
    assert single_interview_email_request(text)


@pytest.mark.parametrize('suffix', [
    'Do not send, but publish it now.',
    'Do not send, but then publish it now.',
    'Do not search, build or publish anything. Then contact the provider.',
    'No product exists, publish it now.',
])
def test_negative_lists_do_not_hide_positive_outreach_commands(suffix):
    from app.chat.intent import single_interview_email_request
    assert not single_interview_email_request('Draft one interview email for review. ' + suffix)
@pytest.mark.parametrize("audience,duration", [("theatre technicians", 30), ("amateur cyclists", 25)])
def test_experiment_duration_reaches_model_and_template_invitation(venture, monkeypatch, audience, duration):
    import asyncio
    import json

    from app.autopilot import llm, outreach
    from app.db.models import GtmContact, VentureDocument
    from app.experiments import outreach_tests
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.state import db
    from core.clock import utcnow

    request = (f"Interview {audience}. Spend 5 minutes preparing your notes. "
        + "Keep your records private. " * 20
        + f"Your method: prepare a private {duration}-minute interview. Ask about the last incident and the records they used.")
    captured = []

    async def ask_json(venture_id, action, system, prompt, *args):
        captured.append((system, prompt))
        return {"drafts": []}

    monkeypatch.setattr(llm, "ask_json", ask_json)
    monkeypatch.setattr(outreach, "site_url", lambda _: None)
    asyncio.run(outreach._llm_drafts("venture", [{"to": "reader@example.test", "pitch": f"Planning for {audience}"}], request))
    system, _ = captured[0]
    assert f"{duration}-minute call" in system and "15-minute call" not in system
    _, body = outreach._template(f"Planning for {audience}", audience, "", request=request)
    assert f"{duration} minutes" in body and "15 minutes" not in body
    _, default = outreach._template(f"Planning for {audience}", audience, "")
    assert "15 minutes" in default
    _, venture_id, workspace_id = venture
    monkeypatch.setattr(outreach_tests, "booking_link", lambda *args, **kwargs: "https://calendar.example.test/interview")
    with db()() as session:
        session.add(GtmContact(id="duration_reader", workspace_id=workspace_id, email="reader@example.test",
            source=f"venture:{venture_id}:founder", enrichment_json='{"founder_import":true}',
            stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    identifier = outreach._save_draft(venture_id, {"to": "reader@example.test", "contact_id": "duration_reader",
        "channel": "email", "state": "manual", "playbook": "problem_interview", "body": body, "subject": "Your recent experience"})
    with db()() as session:
        saved = json.loads(session.get(VentureDocument, identifier).provenance_json)
    assert f"{duration} minutes" in saved["body"] and "15-minute" not in saved["body"]
    assert f"If a {duration}-minute interview works for you" in saved["body"]
    assert "calendar.example.test/interview" in saved["body"] and OPT_OUT in saved["body"]

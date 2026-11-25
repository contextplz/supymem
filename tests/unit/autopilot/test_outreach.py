"""God mode outreach: target list, batch drafts, consented sends from the founder's Gmail with caps, follow-ups,
reply tracking, PMF hypotheses and live-page experiments, the claims guard, chat quiet-down, UTM attribution."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from tests.unit.autopilot.test_god_mode import _on, env as _shared_env
from tests.unit.autopilot.test_lifecycle import _msgs

env = _shared_env  # publish the shared pytest fixture without redefining its import


@pytest.mark.parametrize("previous,current,offer", [
    ("restaurant shift supervisors", "independent restaurant owners", "a supplier comparison notebook"),
    ("competitive runners", "recreational swimmers", "a swimming session diary"),
])
def test_outreach_context_uses_the_confirmed_customer_not_creation_text(env, previous, current, offer):
    from app.autopilot import outreach
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import _next_turn_number
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id = env
    confirm_saved_idea(venture_id)
    with db()() as session:
        venture = session.get(Venture, venture_id)
        venture.raw_idea = f"Build {offer} for {previous}."
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        from tests.company_os_harness import append_idea_revision

        revision = append_idea_revision(session, idea.current_revision_id,
            structured=json.dumps({"customer": current, "offer": offer, "problem": "Important records get misplaced"}),
            summary=f"Build {offer} for {current}.")
        session.add(ClarifierTurn(id="outreach_current_summary", session_id=idea.id, workspace_id=idea.workspace_id,
            venture_id=venture_id, revision_id=revision.id, turn=_next_turn_number(session, idea.id), role="founder",
            kind="answer", text=revision.summary, options="{}", created_at=utcnow()))
        workspace_id = venture.workspace_id
        session.commit()
    workspace, context = outreach._venture(venture_id)
    assert workspace == workspace_id
    assert current in context and offer in context
    assert previous not in context


def _ws(vid):
    from app.db.models import Venture
    from app.state import db

    with db()() as s:
        return s.get(Venture, vid).workspace_id


def _contacts(vid, n, *, domain="gym"):
    from app.db.models import GtmContact
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    ids = []
    with db()() as s:
        for i in range(n):
            c = GtmContact(id=new_id("gtmc"), workspace_id=_ws(vid), email=f"owner{i}@{domain}{i}.example", email_confidence="risky",
                           recipient_geo="US", lawful_basis="legitimate_interest", source=f"venture:{vid}:research", dossier_hook="Found on gym0.example",
                           enrichment_json=json.dumps({"business": f"Gym {i}", "found_at": f"https://{domain}{i}.example/contact"}), stage_changed_at=utcnow(), created_at=utcnow())
            s.add(c)
            ids.append(c.id)
        s.commit()
    return ids


def _sending_address(vid):
    from app.db.models import GtmOutreachSettings
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        settings = session.get(GtmOutreachSettings, _ws(vid))
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=_ws(vid), created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "123 Fixture Lane, Example City, CA 90000, US"
        session.commit()


def _mailbox(vid):
    from app.db.models import GtmEmailAccount
    from app.state import db
    from core.clock import utcnow

    _sending_address(vid)
    with db()() as s:
        s.add(GtmEmailAccount(id="gea_1", workspace_id=_ws(vid), email="founder@gmail.example", composio_user_id=f"gmail:{_ws(vid)}",
                              connected_account_id="ca_live", status="active", created_at=utcnow(), updated_at=utcnow()))
        s.commit()


@pytest.fixture()
def mail(env, monkeypatch):
    """Live-looking mailbox, outbound switched on, no model, Gmail replaced by a recorder."""
    client, vid = env
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "1")
    from app.autopilot import llm, outreach

    async def no_model(*a, **k):
        return None

    monkeypatch.setattr(llm, "ask_json", no_model)
    monkeypatch.setattr(outreach, "site_url", lambda _v: "https://gym.example/")
    sent = []

    def fake_send(box, to, subject, body, thread_id):
        sent.append({"to": to, "subject": subject, "body": body, "thread": thread_id})
        return {"provider_message_id": f"m{len(sent)}", "thread_id": f"t{len(sent)}"}

    monkeypatch.setattr(outreach, "_send_one", fake_send)
    _mailbox(vid)
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings
        from app.gtm.local_mail import ensure_account
        from app.state import db

        update_settings("smtp", {
            "host": "smtp.example.test", "port": 587, "user": "founder@gmail.example",
            "sender_name": outreach.founder_signature(vid), "postal_address": "123 Fixture Lane, Example City, CA 90000, US",
            "imap_host": "imap.example.test",
        }, {"password": "synthetic-mail-password"})
        update_settings("capabilities", {"send_email": True}, {})
        workspace_id = _ws(vid)
        with db()() as session:
            ensure_account(session, workspace_id)
            session.commit()

        def unexpected_smtp(*args, **kwargs):
            pytest.fail("Use a fake SMTP transport before sending in this test.")

        monkeypatch.setattr("smtplib.SMTP", unexpected_smtp)
        monkeypatch.setattr("smtplib.SMTP_SSL", unexpected_smtp)
        monkeypatch.setattr("imaplib.IMAP4_SSL", unexpected_smtp)

    def profile(user_id, connected_account_id):
        if is_local():
            from app.gtm.local_mail import check_binding, settings
            check_binding(user_id, connected_account_id)
            mailbox_email = settings().username
        else:
            mailbox_email = "founder@gmail.example"
        return {"email": mailbox_email, "raw": {}}

    monkeypatch.setattr("app.gtm.supymem.composio_gmail.get_gmail_profile", profile)
    monkeypatch.setattr("app.gtm.supymem.composio_gmail.fetch_query", lambda **kwargs: {"messages": []})
    return client, vid, sent


def _run(coro):
    return asyncio.run(coro)


def _opt_out_line(workspace_id, contact_id, email):
    from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT, unsubscribe_url
    link = unsubscribe_url(workspace_id, contact_id, email)
    return "Stop these emails: " + link if link else REPLY_OPT_OUT


OUTREACH_VENTURES = (
    ("RackSignal", "warehouse supervisors", "Germany", "DE",
     "(something for RackSignal maintenance alerts in Jira idk (rough draft) Audience: warehouse supervisors in Germany, still messy)",
     "a maintenance alert tracker for warehouse supervisors using Jira"),
    ("TrailNest", "hiking families", "Canada", "CA",
     "(something for TrailNest family trail planning with OpenStreetMap idk (rough draft) Audience: hiking families in Canada, still messy)",
     "a family trail planner for hiking families using OpenStreetMap"),
)


@pytest.mark.parametrize("case", OUTREACH_VENTURES)
def test_missing_venture_has_no_founder_proof_and_rejects_invented_claims(env, monkeypatch, case):
    from app.autopilot import claims
    from app.chat import store
    from core.ids import new_id

    statement = f"I've interviewed {case[1]}."
    monkeypatch.setattr(store, "list_messages", lambda *args, **kwargs: [{"role": "user", "body_md": statement}])
    missing_id = new_id("ven")
    words = claims.founder_words(missing_id)
    assert words == "" and claims.founder_proof(missing_id) == ""
    assert claims.violations(statement, founder_words=words)
    assert not claims.violations(statement, founder_words=statement)


def _outreach_venture(client, case, *, pitch=""):
    from app.gtm import journey
    from app.state import db

    name, who, country, _code, raw, _pitch = case
    created = client.post("/ventures", json={"name": name, "raw_idea": raw})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    with db()() as session:
        row = journey._row(session, vid, create=True)
        journey._store(session, row, {"icp": {"who": who, "location": country}, "pitch": pitch})
        session.commit()
    return vid


@pytest.mark.parametrize("case", OUTREACH_VENTURES)
def test_outreach_pitch_context_keeps_latest_unparsed_founder_statement(env, case):
    from app.autopilot import outreach

    client, _ = env
    venture_id = _outreach_venture(client, case)
    assert outreach._venture(venture_id)[1] == case[-2]


@pytest.mark.parametrize("pitch_source", ["saved", "generated", "unavailable"])
def test_outreach_uses_each_ventures_clean_pitch_or_clean_fallback(mail, monkeypatch, pitch_source):
    from types import SimpleNamespace
    from app.autopilot import llm, outreach
    from app.chat import assistant
    from app.credits import metered
    from app.db.models import GtmContact
    from app.gtm import journey, playbooks
    from app.planning import execute
    from app.state import db

    client, _, _ = mail
    monkeypatch.setattr(outreach, "site_url", lambda _vid: None)
    calls, prompts = [], []

    async def compose_pitch(messages, **kwargs):
        calls.append(kwargs["venture_id"])
        if pitch_source == "unavailable":
            raise RuntimeError("offline provider")
        clean = next(case[-1] for case in OUTREACH_VENTURES if case[-2] == messages[-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=clean))])

    async def no_drafts(vid, purpose, system, prompt, *args):
        assert purpose == "outreach.draft_batch"
        prompts.append((vid, prompt))
        return None

    monkeypatch.setattr(metered, "llm", compose_pitch)
    monkeypatch.setattr(llm, "ask_json", no_drafts)
    async def no_review_model(*args, **kwargs):
        return ""
    monkeypatch.setattr(assistant, "_complete", no_review_model)
    for case in OUTREACH_VENTURES:
        name, _who, _country, _code, raw, clean = case
        saved_pitch = "I'm building " + clean if pitch_source == "saved" else ""
        vid = _outreach_venture(client, case, pitch=saved_pitch)
        ident = _contacts(vid, 1, domain=name.lower())[0]
        with db()() as session:
            contact = session.get(GtmContact, ident)
            contact.recipient_geo, contact.dossier_hook = None, "found on a public contact page"
            session.commit()
        reply = _run(outreach.draft_batch(vid))
        draft = outreach.drafts(vid, "draft")[0]
        expected = name + (", " + clean if pitch_source != "unavailable" else "")
        assert "I'm building " + expected + "." in draft["body"]
        message = playbooks.messages({"company": "Public contact"}, "problem_interview",
            clean if pitch_source != "unavailable" else "", venture_name=name)
        assert "I'm building " + expected + "." in message["email"]
        assert raw not in draft["body"] and raw not in prompts[-1][1]
        assert expected in prompts[-1][1]
        assert not any(filler in draft["body"] for filler in ("idk", "something for", "rough draft", "Audience:", "still messy"))
        assert "Found on a public contact page." in draft["body"]
        assert draft["body"] in reply["body_md"]
        fallback = _run(execute._review_drafts(vid, "Draft outreach emails", ""))
        assert expected in fallback and raw not in fallback
        assert not any(filler in fallback for filler in ("idk", "something for", "rough draft", "Audience:", "still messy"))
        with db()() as session:
            cached = journey._json(journey._row(session, vid).provenance_json)
            assert cached["icp"]["who"] == case[1]
            assert cached["pitch"] == (saved_pitch or clean if pitch_source != "unavailable" else "")
        assert _run(outreach.draft_batch(vid)) is None
    assert len(calls) == (0 if pitch_source == "saved" else 2)
    assert len(prompts) == 2


@pytest.mark.parametrize("case", OUTREACH_VENTURES, ids=["b2b-germany", "consumer-canada"])
def test_outreach_geography_filters_discovery_saved_targets_and_send_cards(mail, monkeypatch, case):
    from app.autopilot import llm, outreach
    from app.chat import customers, jarvis
    from app.chat.execute import execute_action
    from app.db.models import GtmContact, VentureDocument
    from app.gtm import journey
    from app.research import explore
    from app.state import db

    client, _, delivered = mail
    vid = _outreach_venture(client, case, pitch=case[-1])
    country, code = case[2:4]
    foreign = "Canada" if country == "Germany" else "Germany"
    tld = "ca" if code == "DE" else "de"
    pages = [
        {"url": f"https://local.{code.lower()}/contact", "title": "Local contact", "excerpt": f"hello@local.{code.lower()} in {country}"},
        {"url": "https://unknown.example/contact", "title": "Unknown contact", "excerpt": "hello@unknown.example", "country": "unknown"},
        {"url": f"https://foreign.{tld}/contact", "title": "Foreign contact", "excerpt": f"hello@foreign.{tld}", "country": code},
        {"url": "https://foreigntext.example/contact", "title": "Foreign contact", "excerpt": f"hello@foreigntext.example Our customers are in {foreign}"},
        {"url": "https://foreignaddress.example/contact", "title": "Foreign contact", "excerpt": "hello@foreignaddress.example", "address": "100 Test Road, Boston, MA 02110"},
    ]

    async def search(*args, **kwargs):
        return {"pages": pages, "hits": []}

    async def no_model(*args, **kwargs):
        return None

    monkeypatch.setattr(explore, "explore_web", search)
    monkeypatch.setattr(llm, "ask_json", no_model)
    _run(outreach.build_targets(vid))
    assert {contact.email for contact in outreach.contacts(vid)} == {f"hello@local.{code.lower()}", "hello@unknown.example"}
    stale = _contacts(vid, 6, domain=case[0].lower())
    evidence = [
        {"found_at": f"https://old.{tld}/contact"},
        {"company_text": f"We operate only in {foreign}"},
        {"address": "100 Test Road, Boston, MA 02110"},
        {"country": foreign},
        {"website": f"https://oldsite.{tld}"},
        {},
    ]
    with db()() as session:
        for ident, facts in zip(stale, evidence):
            contact = session.get(GtmContact, ident)
            contact.recipient_geo, contact.dossier_hook = None, "Found on a public page"
            contact.enrichment_json = json.dumps(facts)
        session.commit()
    for ident in stale:
        with db()() as session:
            email = session.get(GtmContact, ident).email
        outreach._save_draft(vid, {"contact_id": ident, "channel": "email", "to": email, "subject": "Review",
            "body": "Saved for review.", "day": 1, "state": "draft", "batch_id": "old-batch" if ident != stale[-1] else "unknown-batch"})
    eligible = {f"hello@local.{code.lower()}", "hello@unknown.example", f"owner5@{case[0].lower()}5.example"}
    assert {contact.email for contact in outreach.contacts(vid)} == eligible
    _run(outreach.draft_batch(vid))
    assert {draft["to"] for draft in outreach.drafts(vid, "draft")} == eligible
    view = customers._view(vid)
    assert len(customers._pending(view)) == (0 if view["icp"]["mode"] == "consumer" else 3)
    assert all(prospect["email"] in eligible for prospect in view.get("prospects") or [])
    review = _run(execute_action(vid, "outreach.drafts"))
    assert all(f"owner{index}@" not in review["body_md"] for index in range(5))
    approval = jarvis.batch_approve(vid)
    assert "3 draft(s) waiting" in approval["body_md"]
    assert sum(int(action["label"].split()[1]) for action in approval["actions"] if action.get("confirmation")) == 3
    held = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": "old-batch"}))
    assert not delivered
    assert "owner0@" not in held["body_md"]
    with db()() as session:
        assert session.query(VentureDocument).filter_by(venture_id=vid, kind=outreach.KIND).count() == 8
    assert not journey._geography_mismatch(country, "https://unknown.example", "", "unknown", require_match=True)


# ---- 3. target list ----

def test_target_list_keeps_public_business_contacts_with_their_source(env, monkeypatch):
    client, vid = env
    from app.autopilot import llm, outreach
    from app.research import explore

    async def no_model(*a, **k):
        return None

    async def fake_explore(q, **k):
        return {"pages": [
            {"url": "https://irongym.example/contact", "title": "Iron Gym - Contact", "excerpt": "Email hello@irongym.example or noreply@irongym.example. instagram.com/irongym_official and instagram.com/p/xyz"},
            {"url": "https://yelp.com/biz/x", "title": "Yelp", "excerpt": "someone@gmail.com"},  # an aggregator: not the business's own page
            {"url": "https://irongym.example/about", "title": "About", "excerpt": "unrelated@otherdomain.example"}], "hits": []}

    monkeypatch.setattr(llm, "ask_json", no_model)
    monkeypatch.setattr(explore, "explore_web", fake_explore)
    reply = _run(outreach.build_targets(vid))
    rows = outreach.contacts(vid)
    assert sorted(c.email or "@" + json.loads(c.enrichment_json)["instagram"] for c in rows) == ["@irongym_official", "hello@irongym.example"]
    assert all(c.source == f"venture:{vid}:research" and c.lawful_basis == "legitimate_interest" for c in rows)
    assert "1 public business email" in reply["body_md"] and "1 Instagram" in reply["body_md"]
    _run(outreach.build_targets(vid))
    assert len(outreach.contacts(vid)) == 2  # a second search adds nothing already known


def test_articles_and_social_posts_are_evidence_not_contacts_and_greetings_stay_neutral(mail, monkeypatch):
    client, vid, _ = mail
    from app.autopilot import llm, outreach
    from app.research import explore

    async def no_model(*a, **k):
        return None

    async def fake_explore(q, **k):
        return {"pages": [
            {"url": "https://studio.example/instagram-gyms", "title": "Instagram Strategy for Gyms: A Studio Owner's Guide", "excerpt": "hello@studio.example instagram.com/studio"},
            {"url": "https://business.instagram.com/blog", "title": "Grow your gym", "excerpt": "instagram.com/blog"},
            {"url": "https://x.com/someone/status/1", "title": "Someone on X: \"my gym members keep quitting\"", "excerpt": "instagram.com/realgym"}], "hits": []}

    monkeypatch.setattr(llm, "ask_json", no_model)
    monkeypatch.setattr(explore, "explore_web", fake_explore)
    _run(outreach.build_targets(vid))
    rows = outreach.contacts(vid)
    assert [json.loads(c.enrichment_json).get("instagram") for c in rows] == ["realgym"]
    _run(outreach.draft_batch(vid))
    dm = next(d for d in outreach.drafts(vid) if d["channel"] == "instagram")
    assert dm["to"] == "@realgym" and dm["body"].startswith("Hi,\n") and "my gym members" not in dm["body"]


# ---- 2. a batch of drafts ----

@pytest.mark.parametrize("audience,offer,problem,boundaries", [
    ("commercial kitchen maintenance coordinators", "a maintenance logbook", "misplaced inspection records",
     "Use fictional records only. Never request passwords. Leave operating machinery untouched."),
    ("amateur pottery enthusiasts", "a pottery practice journal", "tracking glaze experiments",
     "Keep notes anonymous. Do not collect home addresses. Avoid firing demonstrations."),
], ids=["b2b-maintenance", "consumer-pottery"])
@pytest.mark.parametrize("failure", ["missing-copy", "invalid-copy"])
def test_long_interview_plan_fits_complete_email_without_losing_boundaries(
        mail, monkeypatch, audience, offer, problem, boundaries, failure):
    from app.autopilot import outreach
    from app.gtm import journey

    _, venture_id, sent = mail
    _contacts(venture_id, 1, domain="research")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _: offer)
    monkeypatch.setattr(outreach, "site_url", lambda _: "")
    schedule = "Arrange calls between December 14 and December 18, 2026."
    operations = (
        "Run a private interview test from December 14 to December 18, 2026, using UTC until you choose your timezone. "
        "On days 1 and 2, recruit volunteers through public community introductions and arrange individual conversations around their schedules. "
        "Recruit seven volunteers through public community introductions and arrange individual conversations around their schedules. "
        "Write down recent examples of the problem, the time spent on each workaround, and how frequently it happens. "
        "Compare the accounts after all conversations and separate interest in the proposal from claims about product performance. "
        "Review the evidence at the end of the week and decide whether another round of research is worthwhile. "
        "Their reported workaround is context, not independent evidence of demand. "
        "Then describe a hypothetical product for discussing the idea rather than showing a working solution. "
        "Request optional permission for follow-up, without promising availability or performance. "
        "On day 7, summarize anonymized responses and decide what uncertainty to study next. "
        "Treat interview statements as self-reports, not verified usage. ")
    script = f"Ask: 'Tell me about the latest time you dealt with {problem}. How did you respond?'"
    request = (f"Your ask: a 25-minute call. Interview invitations for {audience} about {problem}. "
        f"Test: Learn about recent workarounds. Plan: {schedule} {operations} "
        f"{script} The offer is a proposal only. {boundaries} What to record: Anonymous accounts of workarounds.")

    async def modeled(*args, **kwargs):
        if failure == "missing-copy":
            return {}
        return {"owner0@research0.example": {"subject": "Your recent experience",
            "body": "You can skip any topic. " * 30 + "Could you share your experience in a 25-minute call?"}}

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    _run(outreach.draft_batch(venture_id, request=request, review_only=True))
    draft, = outreach.drafts(venture_id, "draft")
    body = draft["body"]
    assert draft["source"] == "template" and draft["requires_batch_approval"] is True
    assert len(body.split()) <= 120
    assert problem in body and schedule in body and "The offer is a proposal only." in body
    assert boundaries in body and outreach._interview_minutes(body) == "25"
    assert "private" in body and "December 14 to December 18, 2026 UTC" in body
    assert "Optional follow-up; no promises of availability or performance." in body
    assert "Keep notes anonymous." in body
    assert operations not in body and "What to record:" not in body and "Plan:" not in body
    assert "Tell me about" not in body and "How did you respond" not in body
    assert "reported workaround" not in body
    assert "Your email address is listed publicly on research0.example." in body
    assert outreach.OPT_OUT in body and _opt_out_line(_ws(venture_id), draft["contact_id"], draft["to"]) in body and "From:" in body
    assert outreach.founder_signature(venture_id) in body
    assert "123 Fixture Lane, Example City, CA 90000, US" in body
    journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
    assert not outreach.drafts(venture_id, "manual", "review_staging") and sent == []
    with pytest.raises(ValueError, match="Ask me to shorten.*keeping your limits"):
        outreach._template(offer, "Reader", "", request=request, word_budget=10)


@pytest.mark.parametrize("audience,offer,limit", [
    ("workshop tool custodians", "a tool inspection notebook", "Leave production equipment untouched."),
    ("terrarium hobbyists", "a terrarium watering planner", "Keep household photographs out of the conversation."),
], ids=["b2b-tools", "consumer-terrariums"])
@pytest.mark.parametrize("failure", ["missing-copy", "wrong-duration", "invalid-copy"])
@pytest.mark.parametrize("schedule", [
    "Hold calls between December 7 and December 11, 2026.",
    "Hold calls between 7 December and 11 December 2026.",
    "Arrange calls next month.",
    "",
], ids=["dated-window", "day-first-window", "relative-window", "default-window"])
def test_prepared_plan_rejects_templates_that_lose_its_limits(mail, monkeypatch, audience, offer, limit, failure, schedule):
    from app.autopilot import outreach
    from app.gtm import journey

    _, venture_id, sent = mail
    _contacts(venture_id, 1, domain="invitation")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _: offer)
    monkeypatch.setattr(outreach, "site_url", lambda _: "")
    request = (f"Your ask: a 25-minute call. Interview invitations for {audience} about missing notes. "
        "Test: You can learn about recent workarounds. "
        f"Plan: {schedule} The offer is a proposal only. "
        f"Use synthetic examples and never request login credentials. {limit} "
        "Ask where they borrow supplies; keep your notes anonymous. Do not ask for account identifiers. "
        "Ask about preferred paper sizes. "
        "What to record: Anonymous descriptions of recent workarounds.")

    async def modeled(target, recipients, angle):
        assert target == venture_id and angle == request
        if failure == "missing-copy":
            return {}
        minutes = "10" if failure == "wrong-duration" else "25"
        body = f"Could you share your recent experience in a {minutes}-minute call?"
        if failure == "invalid-copy":
            body = "You can leave any topic unanswered. " * 25 + body
        return {recipient["to"]: {"subject": "Your recent experience", "body": body} for recipient in recipients}

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    _run(outreach.draft_batch(venture_id, request=request, review_only=True))
    draft, = outreach.drafts(venture_id, "draft")
    body = draft["body"]
    assert draft["source"] == "template" and draft["requires_batch_approval"] is True
    assert "The offer is a proposal only." in body and "I'm building" not in body
    assert "Use synthetic examples and never request login credentials." in body and limit in body
    assert "Keep your notes anonymous." in body and "Do not ask for account identifiers." in body
    assert "borrow supplies" not in body and "preferred paper sizes" not in body
    assert "What to record:" not in body and "Plan:" not in body
    assert outreach._interview_minutes(body) == "25"
    if schedule:
        assert schedule in body and "this week" not in body
    else:
        assert "Would a short call this week work?" in body
    assert "Your email address is listed publicly on invitation0.example." in body
    assert outreach.OPT_OUT in body and _opt_out_line(_ws(venture_id), draft["contact_id"], draft["to"]) in body and "From:" in body
    assert "123 Fixture Lane, Example City, CA 90000, US" in body
    journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
    assert not outreach.drafts(venture_id, "manual", "review_staging")
    assert sent == []


def test_drafting_writes_a_batch_with_utm_link_optout_and_never_an_unbuilt_claim(mail, monkeypatch):
    client, vid, _ = mail
    from app.autopilot import outreach

    _contacts(vid, 12)

    async def modeled(venture_id, chunk, request):
        out = {c["to"]: {"to": c["to"], "subject": f"Hi {c['business']}", "body": f"Hi, I saw {c['business']}. See https://evil.example now."}
               for c in chunk}
        bad = chunk[0]["to"]  # the first of each chunk of 10
        out[bad]["body"] = "Our app automatically sends invoices for you."  # claims a feature that does not exist
        return out

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    reply = _run(outreach.draft_batch(vid, n=12))
    ds = outreach.drafts(vid, "draft")
    assert len(ds) == 12 and reply["actions"][0]["confirmation"]["action"] == "outreach.send"
    assert all("utm_source=outreach" in d["body"] and "utm_content=" in d["body"] and "no thanks" in d["body"] for d in ds)
    assert not any("evil.example" in d["body"] for d in ds)  # a model never invents links
    assert [d["source"] for d in ds].count("template") == 2 and "automatically sends" not in " ".join(d["body"] for d in ds)
    assert _run(outreach.draft_batch(vid, n=12)) is None  # everyone already has a draft
    assert len(outreach.drafts(vid)) == 12


def test_batch_size_is_capped_and_a_missing_model_still_drafts(mail):
    client, vid, _ = mail
    from app.autopilot import outreach

    _contacts(vid, 35)
    _run(outreach.draft_batch(vid, n=99))
    assert len(outreach.drafts(vid)) == outreach.BATCH_MAX  # no model: honest templates, never nothing
    assert {d["source"] for d in outreach.drafts(vid)} == {"template"}


@pytest.mark.parametrize("code", ["model_not_configured", "provider_outcome_unknown", "bad_api_key", "out_of_credits"])
def test_pitch_model_absence_falls_back_without_swallowing_provider_failures(mail, monkeypatch, code):
    _, vid, sent = mail
    from app.autopilot import outreach
    from app.gtm import playbooks
    from llm_gateway.errors import ProviderCallError

    _contacts(vid, 1)
    failure = ProviderCallError(code, "Pitch model unavailable")

    async def unavailable(*args):
        raise failure

    monkeypatch.setattr(playbooks, "pitch", unavailable)
    if code == "model_not_configured":
        _run(outreach.draft_batch(vid))
        drafts = outreach.drafts(vid)
        assert len(drafts) == 1 and drafts[0]["source"] == "template"
    else:
        with pytest.raises(ProviderCallError) as caught:
            _run(outreach.draft_batch(vid))
        assert caught.value is failure
        assert outreach.drafts(vid) == []
    assert sent == []


# ---- 1. sending: consent, caps, dedupe, opt-out, every send a touch ----

def test_nothing_is_sent_without_allow_sending_or_a_batch_approval(mail):
    client, vid, sent = mail
    from app.autopilot import guard, outreach

    _contacts(vid, 3)
    _run(outreach.draft_batch(vid))
    _on(client, vid)
    assert guard.check(vid, "outreach.send").reason == "sending_not_allowed"  # God mode alone never sends
    assert _run(outreach.send_batch(vid, {}))["kind"] == "needs_you" and not sent
    assert guard.check(vid, "outreach.send", human_approved=True).ok  # a batch the founder approved


def test_approved_batch_sends_through_gmail_once_per_person_and_counts_for_the_pmf_loop(mail):
    client, vid, sent = mail
    from app.autopilot import guard, outreach, pmf

    _contacts(vid, 3)
    _run(outreach.draft_batch(vid))
    batch = outreach.drafts(vid)[0]["batch_id"]
    out = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": batch}))
    assert "Sent 3 outreach" in out["body_md"] and len(sent) == 3 and all(s["thread"] is None for s in sent)
    assert pmf.read_metric(vid, "outbound_touches") == 3  # every send is a gtm_touch
    assert guard.get_settings(vid)["sends_today"] == 3
    assert outreach.send_slots_used_today(vid) == 3  # Confirmed touches replace, rather than add to, reservations.
    assert _run(outreach.send_batch(vid, {"approved_by_founder": True}))["kind"] == "wait" and len(sent) == 3
    # the same recipient is never emailed twice even if a second draft appears
    c = outreach.contacts(vid)[0]
    outreach._save_draft(vid, {"contact_id": c.id, "to": c.email, "channel": "email", "day": 1, "subject": "again", "body": "again", "state": "draft", "batch_id": "x"})
    out = _run(outreach.send_batch(vid, {"approved_by_founder": True}))
    assert len(sent) == 3 and "skipped 1" in out["body_md"]


@pytest.mark.parametrize("domain,topic", [("freight-planning", "warehouse handovers"), ("seed-sharing", "backyard planting")])
def test_successful_retry_clears_saved_send_error_and_failure_banner(mail, monkeypatch, domain, topic):
    from app.autopilot import outreach
    from app.chat import proactive
    from app.db.models import VentureDocument
    from core.clock import utcnow

    _, venture_id, sent = mail
    _contacts(venture_id, 1, domain=domain)
    _run(outreach.draft_batch(venture_id))
    draft = outreach.drafts(venture_id)[0]
    outreach._save_draft(venture_id, draft | {"subject": f"A question about {topic}", "error": "composio_rate_limited"}, draft["doc_id"])
    result = _run(outreach.send_batch(venture_id, {"approved_by_founder": True, "batch_id": draft["batch_id"]}))
    assert "Sent 1 outreach" in result["body_md"] and len(sent) == 1
    saved = next(item for item in outreach.drafts(venture_id) if item["doc_id"] == draft["doc_id"])
    assert saved["state"] == "sent" and "error" not in saved
    from app.state import db
    with db()() as session:
        row = session.get(VentureDocument, draft["doc_id"])
        assert "composio_rate_limited" not in row.provenance_json
        assert not any(event["key"].startswith("send_blocked:") for event in proactive._gtm_events(session, venture_id, utcnow()))


def test_chat_drafts_require_exact_batch_review_even_with_god_sending_on(mail):
    client, vid, sent = mail
    from app.autopilot import guard, outreach

    _on(client, vid, allow_send=True, send_max_per_day=2)
    before = guard.get_settings(vid)
    _contacts(vid, 3)
    drafted = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft", "payload": {}})
    assert drafted.status_code == 200 and "Nothing is sent yet" in drafted.json()["body_md"]
    assert "credit" not in drafted.json()["body_md"].lower()
    assert all(action["confirmation"]["action"] == "outreach.send" for action in drafted.json()["actions"] if action.get("confirmation"))
    ds = outreach.drafts(vid, "draft")
    assert len(ds) == 3 and all(d.get("requires_batch_approval") is True for d in ds)
    assert not sent and not any(m.action == "outreach.send" for m in outreach.moves(vid))
    batch = ds[0]["batch_id"]
    for payload in [{}, {"approved_by_founder": True}, {"batch_id": batch},
                    {"approved_by_founder": True, "batch_id": "other-batch"}]:
        _run(outreach.send_batch(vid, payload))
        assert not sent and len(outreach.drafts(vid, "draft")) == 3
    assert outreach.ask_approval(vid) is True
    assert outreach.ask_approval(vid) is False
    send_review = next(a["confirmation"] for a in drafted.json()["actions"] if a.get("confirmation"))
    assert send_review["resulting_state"] == {"batch_id": batch, "approved_by_founder": True}
    response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.send", "payload": send_review["resulting_state"]})
    assert response.status_code == 200 and "Sent 2 outreach" in response.json()["body_md"]
    assert len(sent) == 2 and len(outreach.drafts(vid, "draft")) == 1
    _run(outreach.send_batch(vid, {}))
    _run(outreach.send_batch(vid, send_review["resulting_state"]))
    assert len(sent) == 2  # Held remainder stays reviewed and the daily cap still applies.
    after = guard.get_settings(vid)
    assert all(after[key] == before[key] for key in ("god_mode", "allow_send", "send_max_per_day", "daily_cap_credits"))


def test_automatic_batch_can_send_without_releasing_a_review_only_batch(mail):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True)
    _contacts(vid, 1, domain="review")
    _run(outreach.draft_batch(vid, review_only=True))
    held = outreach.drafts(vid, "draft")[0]
    _contacts(vid, 1, domain="automatic")
    _run(outreach.draft_batch(vid))
    ordinary = next(d for d in outreach.drafts(vid, "draft") if not d.get("requires_batch_approval"))
    assert ordinary["batch_id"] != held["batch_id"]
    assert any(m.action == "outreach.send" for m in outreach.moves(vid))
    _run(outreach.send_batch(vid, {}))
    assert [s["to"] for s in sent] == [ordinary["to"]]
    assert outreach.drafts(vid, "draft")[0]["doc_id"] == held["doc_id"]
    assert not any(m.action == "outreach.send" for m in outreach.moves(vid))


def test_review_batch_is_not_visible_to_approval_or_sending_mid_generation(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True)
    _contacts(vid, 3)
    monkeypatch.setattr(outreach, "CHUNK", 2)
    observed = []

    async def modeled(_vid, _chunk, _request):
        staged = outreach.drafts(vid, "review_staging")
        if staged:
            observed.append(len(staged))
            assert len(staged) == 2 and not outreach.drafts(vid, "draft", "manual")
            assert outreach.ask_approval(vid) is False
            assert not any(m.action == "outreach.send" for m in outreach.moves(vid))
            await outreach.send_batch(vid, {})
            await outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": staged[0]["batch_id"]})
            assert not sent and not any("ready to go from your Gmail" in m for m in _msgs(vid))
        return {}

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    reply = _run(outreach.draft_batch(vid, review_only=True))
    assert observed == [2] and not outreach.drafts(vid, "review_staging")
    assert len(outreach.drafts(vid, "draft")) == 3 and not sent
    review = reply["actions"][0]["confirmation"]
    assert review["interpretation"] == "Send 3 outreach email(s) from your own Gmail."
    assert outreach.ask_approval(vid) is True
    assert any("I have 3 outreach email(s) ready" in m for m in _msgs(vid))


def test_failed_review_batch_is_abandoned_safely_and_explicit_retry_covers_all_contacts(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True)
    contact_ids = _contacts(vid, 2)
    monkeypatch.setattr(outreach, "CHUNK", 1)

    async def modeled(_vid, _chunk, _request):
        if outreach.drafts(vid, "review_staging"):
            raise RuntimeError("owned generation failure")
        return {}

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    with pytest.raises(RuntimeError, match="owned generation failure"):
        _run(outreach.draft_batch(vid, review_only=True))
    abandoned = outreach.drafts(vid, "review_abandoned")
    assert len(abandoned) == 1 and not outreach.drafts(vid, "review_staging", "draft", "manual")
    assert outreach.ask_approval(vid) is False
    _run(outreach.send_batch(vid, {}))
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": abandoned[0]["batch_id"]}))
    assert not sent and not any("ready to go from your Gmail" in m for m in _msgs(vid))

    async def recovered(_vid, _chunk, _request):
        return {}

    monkeypatch.setattr(outreach, "_llm_drafts", recovered)
    retry = client.post(f"/ventures/{vid}/chat/confirm", json={"action": "outreach.draft", "payload": {}})
    assert retry.status_code == 200 and "Drafted 2 personal email(s)" in retry.json()["body_md"]
    assert "credit" not in retry.json()["body_md"].lower()
    assert all(action["confirmation"]["action"] == "outreach.send" for action in retry.json()["actions"] if action.get("confirmation"))
    ds = outreach.drafts(vid, "draft")
    assert {d["contact_id"] for d in ds} == set(contact_ids)
    assert all(d["requires_batch_approval"] is True and d["batch_id"] != abandoned[0]["batch_id"] for d in ds)
    assert not outreach.drafts(vid, "review_staging") and not sent
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": abandoned[0]["batch_id"]}))
    assert not sent and len(outreach.drafts(vid, "draft")) == 2
    assert outreach.ask_approval(vid) is True
    assert any("I have 2 outreach email(s) ready" in m for m in _msgs(vid))


def test_publication_exception_does_not_abandon_or_release_uncertain_review_work(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True)
    _contacts(vid, 2)
    publish = outreach._publish_review_batch

    def lost_reply(_vid, batch):
        publish(_vid, batch)
        raise RuntimeError("owned publication acknowledgement failure")

    monkeypatch.setattr(outreach, "_publish_review_batch", lost_reply)
    with pytest.raises(RuntimeError, match="publication acknowledgement failure"):
        _run(outreach.draft_batch(vid, review_only=True))
    assert not outreach.drafts(vid, "review_abandoned")
    ds = outreach.drafts(vid, "draft")
    assert len(ds) == 2 and all(d["requires_batch_approval"] is True for d in ds)
    assert _run(outreach.draft_batch(vid, review_only=True)) is None  # No duplicate generation after uncertain publication.
    _run(outreach.send_batch(vid, {}))
    assert not sent and not any(m.action == "outreach.send" for m in outreach.moves(vid))


def test_review_only_email_does_not_authorize_automatic_followups(mail):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True)
    _contacts(vid, 1)
    _run(outreach.draft_batch(vid, review_only=True))
    first_batch = outreach.drafts(vid, "draft")[0]["batch_id"]
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": first_batch}))
    _age_sent(vid, 4)
    assert outreach.schedule_followups(vid) == 1
    followup = outreach.drafts(vid, "draft")[0]
    assert followup["requires_batch_approval"] is True and followup["batch_id"] != first_batch
    assert not any(m.action == "outreach.send" for m in outreach.moves(vid))
    _run(outreach.send_batch(vid, {}))
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": first_batch}))
    assert len(sent) == 1
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": followup["batch_id"]}))
    assert len(sent) == 2 and sent[-1]["thread"] == "t1"


def test_daily_cap_and_opt_outs_are_floors(mail):
    client, vid, sent = mail
    from app.autopilot import outreach
    from app.db.models import GtmSuppression
    from app.state import db
    from core.clock import utcnow

    _on(client, vid)
    off = client.put(f"/ventures/{vid}/autopilot", json={"allow_send": True}).json()
    assert off["allow_send"] and off["send_max_per_day"] == 15
    client.put(f"/ventures/{vid}/autopilot", json={"allow_send": True, "send_max_per_day": 2})
    _contacts(vid, 5)
    blocked = outreach.contacts(vid)[0]
    with db()() as s:
        s.add(GtmSuppression(id="sup1", workspace_id=_ws(vid), email=blocked.email, reason="asked", source="opt_out", created_at=utcnow()))
        s.commit()
    _run(outreach.draft_batch(vid))
    out = _run(outreach.send_batch(vid, {}))  # Allow sending is on: no card needed
    assert len(sent) == 2 and blocked.email not in [s["to"] for s in sent] and "0 of today's 2" in out["body_md"]
    assert _run(outreach.send_batch(vid, {}))["kind"] == "wait"  # the cap holds


def test_without_a_gmail_or_a_deployment_switch_nothing_goes_out(env, monkeypatch):
    client, vid = env
    from app.autopilot import llm, outreach

    async def no_model(*a, **k):
        return None

    monkeypatch.setattr(llm, "ask_json", no_model)
    _contacts(vid, 1)
    _run(outreach.draft_batch(vid))
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_capabilities import blocked_message
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"send_email": False}, {})
        assert blocked_message("send_email") in _run(outreach.send_batch(vid, {"approved_by_founder": True}))["body_md"]
        update_settings("capabilities", {"send_email": True}, {})
        out = _run(outreach.send_batch(vid, {"approved_by_founder": True}))
        assert blocked_message("send_email") in out["body_md"] and "Your mail account" in out["body_md"]
        assert len(outreach.drafts(vid, "draft")) == 1
        return
    monkeypatch.delenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", raising=False)
    assert "switched off" in _run(outreach.send_batch(vid, {"approved_by_founder": True}))["body_md"]
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "1")
    out = _run(outreach.send_batch(vid, {"approved_by_founder": True}))
    assert out["kind"] == "needs_you" and out["actions"][0]["label"] == "Connect Gmail"
    assert len(outreach.drafts(vid, "draft")) == 1  # kept


# ---- 4. follow-ups ----

def _age_sent(vid, days):
    from app.autopilot import outreach
    from core.clock import utcnow

    for d in outreach.drafts(vid, "sent"):
        outreach._save_draft(vid, d | {"sent_at": (utcnow() - timedelta(days=days)).isoformat()}, d["doc_id"])


@pytest.mark.parametrize("domain", ["warehouse", "family-gardens"])
def test_follow_ups_1_2_4_7_days_go_to_non_replies_in_the_same_thread(mail, monkeypatch, domain):
    client, vid, sent = mail
    from app.autopilot import outreach
    from app.core.local_mode import is_local

    assert outreach.FOLLOWUP_DAYS == (2, 4, 7)  # the default; this test runs the persistent 1/2/4/7 choice
    monkeypatch.setattr(outreach, "FOLLOWUP_DAYS", (1, 2, 4, 7))

    _contacts(vid, 2, domain=domain)
    _run(outreach.draft_batch(vid))
    _run(outreach.send_batch(vid, {"approved_by_founder": True}))
    assert outreach.schedule_followups(vid) == 0  # too early
    _age_sent(vid, 1)
    if is_local():
        assert outreach.schedule_followups(vid) == 0
        assert len(sent) == 2 and not outreach.drafts(vid, "draft")
    _on(client, vid, allow_send=True)
    assert outreach.schedule_followups(vid) == 2
    assert outreach.schedule_followups(vid) == 0  # not drafted twice
    # one of them replied before the follow-up went out: only the other is followed up
    from app.gtm.supymem import pipeline

    pipeline.apply_reply(outreach.contacts(vid)[0].id, body="yes", classification="human_positive")
    sent.clear()
    out = _run(outreach.send_batch(vid, {"approved_by_founder": True}))
    assert len(sent) == 1 and sent[0]["thread"] and sent[0]["subject"].startswith("Re:") and "no thanks" in sent[0]["body"]
    assert "Sent 1" in out["body_md"]
    bodies = [sent[0]["body"]]
    for after in (2, 4, 7):
        _age_sent(vid, after)
        assert outreach.schedule_followups(vid) == 1  # only the one still silent, one step at a time
        assert outreach.schedule_followups(vid) == 0
        _run(outreach.send_batch(vid, {"approved_by_founder": True}))
        bodies.append(sent[-1]["body"])
    assert [d["day"] for d in outreach.drafts(vid, "sent") if d["to"] == sent[0]["to"]] == [1, 2, 3, 5, 8]
    assert len({b.split("\n\n")[1] for b in bodies}) == 4 and "close the loop" in bodies[-1]
    assert not any("\u2014" in b or "bump" in b.lower() for b in bodies)
    _age_sent(vid, 10)
    assert outreach.schedule_followups(vid) == 0  # the sequence ends at day 7


# ---- 5. replies ----

@pytest.mark.parametrize("domain", ["warehouse", "family-gardens"])
def test_replies_become_touches_alerts_and_pmf_metric_and_a_no_stops_everything(mail, monkeypatch, domain):
    client, vid, sent = mail
    from app.autopilot import outreach, pmf
    from app.core.local_mode import is_local
    from app.gtm.supymem import composio_gmail

    _contacts(vid, 3, domain=domain)
    _run(outreach.draft_batch(vid))
    _run(outreach.send_batch(vid, {"approved_by_founder": True}))
    threads = {row["to"]: f"t{i + 1}" for i, row in enumerate(sent)}
    interested, opted_out = f"owner0@{domain}0.example", f"owner1@{domain}1.example"
    inbox = {interested: [{"id": "reply-0", "threadId": threads[interested], "snippet": "Sounds good, let's talk Tuesday"}],
             opted_out: [{"id": "reply-1", "threadId": threads[opted_out], "snippet": "Not interested, please stop emailing"}]}
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [
        message for messages in inbox.values() for message in messages if message["threadId"] == kwargs["thread_id"]]})
    if is_local():
        from app.core.local_secrets import update_settings

        assert _run(outreach.poll_replies(vid, force=True)) == 0
        update_settings("capabilities", {"read_replies": True}, {})
    assert _run(outreach.poll_replies(vid, force=True)) == 2
    assert pmf.read_metric(vid, "outbound_replies") == 2
    assert _run(outreach.poll_replies(vid, force=True)) == 0  # already logged
    msgs = _msgs(vid)
    assert any(interested in m and "interested" in m for m in msgs)
    assert any(opted_out in m and "stopped" in m for m in msgs)
    from app.db.models import GtmSuppression
    from app.state import db
    from sqlalchemy import select

    with db()() as s:
        assert s.scalar(select(GtmSuppression.email).where(GtmSuppression.email == opted_out)) == opted_out
    _age_sent(vid, 7)
    _on(client, vid, allow_send=True)
    assert outreach.schedule_followups(vid) == 1  # only the contact who has not answered


# ---- God mode: moves, approval card, quiet chat ----

def test_god_mode_builds_targets_then_drafts_then_asks_or_sends(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    assert [m.action for m in outreach.moves(vid)] == ["outreach.targets"]
    _contacts(vid, 4)
    mv = outreach.moves(vid)
    assert [(m.action, m.payload.get("batch")) for m in mv] == [("outreach.draft", True)]
    _run(outreach.draft_batch(vid))
    _on(client, vid)
    assert outreach.moves(vid) == []  # drafts wait for consent; no move sends them
    assert outreach.ask_approval(vid) is True and outreach.ask_approval(vid) is False  # one card per batch
    assert any("ready to go from your Gmail" in m for m in _msgs(vid))
    client.put(f"/ventures/{vid}/autopilot", json={"allow_send": True})
    assert [m.action for m in outreach.moves(vid)] == ["outreach.send"]
    assert outreach.ask_approval(vid) is False  # no card when sending is allowed


def test_suggestion_go_ahead_cannot_approve_sending(mail):
    client, vid, sent = mail
    from app.autopilot import outreach

    _contacts(vid, 3)
    _run(outreach.draft_batch(vid))
    out = _run(_approved_send(vid))
    assert out["reason"] == "needs_you" and out["ok"] is False
    assert out["done"] == [] and out["effects"]["jobs"] == []
    assert sent == []
    from app.db.models import GtmTouch
    from app.state import db
    from sqlalchemy import select

    with db()() as session:
        touches = list(session.scalars(select(GtmTouch).where(
            GtmTouch.workspace_id == _ws(vid), GtmTouch.logged_by == f"outreach:{vid}")))
        assert touches == []
    assert _run(_approved_send(vid))["reason"] == "needs_you"
    assert sent == []


async def _approved_send(vid):
    from app.autopilot.actions import execute_proposed_action

    return await execute_proposed_action(vid, {"kind": "outreach.send", "payload": {}, "title": "Send"}, None)


def test_chat_hears_only_meaningful_events_not_every_tick(env, monkeypatch):
    client, vid = env
    from app.autopilot import service
    from app.chat.store import list_messages
    from core import clock

    now = clock.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(clock, "_clock", lambda: now)

    service._announce(vid, ["outreach.draft", "pmf.iterate", "gtm.diagnosis"])
    assert not any("Autopilot update" in m for m in _msgs(vid))
    service.record(vid, "plan.approve", "Historical activity", outcome="done")
    service._announce(vid, ["plan.approve"])
    assert not any("Autopilot update" in m for m in _msgs(vid))
    service.record(vid, "plan.approve", "Approved the plan", outcome="completed")
    service._announce(vid, ["outreach.draft", "plan.approve"])
    service._announce(vid, ["plan.approve"])  # the same update is not posted twice
    original = next(m for m in list_messages(vid) if m["kind"] == "autopilot_update")
    service.record(vid, "outreach.send", "Sent the approved batch", outcome="completed")
    service._announce(vid, ["outreach.send"])
    service.record(vid, "plan.approve", "Approved the revised plan", outcome="completed")
    service._announce(vid, ["plan.approve", "outreach.send"])
    updates = [m for m in list_messages(vid) if m["kind"] == "autopilot_update"]
    assert len(updates) == 1 and updates[0]["id"] == original["id"]
    assert all(text in updates[0]["body_md"] for text in (
        "Approved the plan", "Sent the approved batch", "Approved the revised plan"))
    assert "Historical activity" not in updates[0]["body_md"]
    now += timedelta(days=1)
    service.record(vid, "outreach.send", "Sent today's batch", outcome="completed")
    service._announce(vid, ["outreach.send"])
    updates = [m for m in list_messages(vid) if m["kind"] == "autopilot_update"]
    assert len(updates) == 2
    today = next(m for m in updates if "Sent today's batch" in m["body_md"])
    assert "Approved the plan" not in today["body_md"]


# ---- 6. hypotheses, live-page experiments, claims guard ----

def test_pmf_hypotheses_come_from_the_brief_and_fall_back_when_dishonest(env, monkeypatch):
    client, vid = env
    from app.autopilot import llm, pmf

    async def good(*a, **k):
        return {"change": "Lead with the missed-payment pain boutique gym owners describe in their own words."}

    monkeypatch.setattr(llm, "ask_json", good)
    v = pmf.BY_NAME["positioning"]
    assert "boutique gym" in _run(pmf.variant_text(vid, v, 0, "raw idea sentence", []))

    async def liar(*a, **k):
        return {"change": "Say we already have 500 customers and our app automatically sends invoices."}

    monkeypatch.setattr(llm, "ask_json", liar)
    assert _run(pmf.variant_text(vid, v, 0, "invoice chaser", [])) == v.variants[0].format(idea="invoice chaser")


def test_a_live_page_experiment_goes_through_build_then_a_guarded_publish(env, monkeypatch):
    client, vid = env
    from app.autopilot import pmf

    monkeypatch.setattr("app.autopilot.pmf.read_metric", lambda _v, m: 0)
    builds = {"id": "bld_1", "ready": True}
    monkeypatch.setattr(pmf, "accepted_build", lambda _v: (builds["id"], builds["ready"]))
    ran = []

    async def run(name, payload):
        ran.append((name, payload))
        return True

    out = _run(pmf.advance(vid, run, allow_send=False, idea="x", live=True))
    first = next(i for i in out if i["variable"] == "positioning")
    assert ran[0][0] == "build.prepare" and "early access" in ran[0][1]["hint"] and first["waiting_publish"]  # honesty rules ride along
    assert "site.publish" not in [n for n, _ in ran]  # nothing public until a new build is accepted
    _run(pmf.advance(vid, run, allow_send=False, idea="x", live=True))
    assert "site.publish" not in [n for n, _ in ran]  # same build as before the change
    builds["id"] = "bld_2"
    _run(pmf.advance(vid, run, allow_send=False, idea="x", live=True))
    assert ("site.publish", {}) in ran
    assert not [i for i in pmf.journal(vid) if i["variable"] == "positioning"][0]["waiting_publish"]  # the clock starts at publish


def test_claims_guard():
    from app.autopilot import claims

    assert claims.violations("Join the early access waitlist. We will send reminders soon.") == []
    assert claims.violations("Trusted by 500 gyms. Our app automatically sends reminders.")
    assert claims.violations("Our platform tracks every payment.")  # present tense, unbuilt
    assert not claims.violations("Our platform tracks every payment.", product_live=True)
    assert claims.violations("Save 40% more time.")


def test_autopilot_will_not_publish_copy_that_claims_unbuilt_features(env, monkeypatch):
    client, vid = env
    from app.autopilot import claims, guard

    _on(client, vid)
    monkeypatch.setattr(claims, "site_violations", lambda _v: ["claims_unbuilt_feature: We automate your invoices."])
    assert guard.check(vid, "site.publish").reason.startswith("public_claims_unverified")
    assert guard.check(vid, "site.publish", human_approved=True).ok  # the founder owns their own page
    monkeypatch.setattr(claims, "site_violations", lambda _v: [])
    assert guard.check(vid, "site.publish").ok


# ---- 8. attribution ----

@pytest.mark.hosted_only
def test_beacon_captures_utm_and_sources_group_by_campaign(env):
    from app.product_analytics.beacon import script_tag

    html = script_tag(key="pk_app_x", endpoint="https://gateway.supymem.com/e")
    assert "utm_source" in html and "utm_campaign" in html and "viewProps.referrer_host" in html
    from app.autopilot import outreach

    link = outreach.utm_link("https://gym.example/?a=1", "ven_abcdefgh12345678", "b0102")
    assert link.startswith("https://gym.example/?a=1&utm_source=outreach&utm_medium=email&utm_campaign=12345678&utm_content=b0102")


def test_traffic_sources_rollup(env):
    client, vid = env
    from app.db.supymem_models import AppEvent, FounderApp
    from app.product_analytics.service import traffic_sources
    from app.state import db
    from core.clock import utcnow

    with db()() as s:
        s.add(FounderApp(id="app_x", workspace_id=_ws(vid), venture_id=vid, slug="app-x", name="x", kind="web",
                         created_at=utcnow(), updated_at=utcnow()))
        s.commit()
        for props in ({"utm_source": "outreach", "utm_campaign": "c1"}, {"utm_source": "outreach", "utm_campaign": "c1"}, {"referrer_host": "t.co"}, {}):
            s.add(AppEvent(app_id="app_x", workspace_id=_ws(vid), ts=utcnow(), name="landing_view", props=json.dumps(props)))
        s.commit()
    assert traffic_sources("app_x") == [{"source": "outreach", "campaign": "c1", "views": 2},
                                        {"source": "t.co", "campaign": "", "views": 1}, {"source": "direct", "campaign": "", "views": 1}]


def test_same_sender_without_owned_thread_is_not_a_venture_reply(mail, monkeypatch):
    _, vid, _ = mail
    from app.autopilot import outreach, pmf
    from app.gtm.supymem import composio_gmail
    _contacts(vid, 1)
    _run(outreach.draft_batch(vid))
    _run(outreach.send_batch(vid, {'approved_by_founder': True}))
    monkeypatch.setattr(composio_gmail, 'fetch_thread', lambda **kwargs: {"messages": [
        {'id': 'other-campaign-message', 'threadId': 'foreign-thread', 'snippet': 'Synthetic unrelated incoming message'}]})
    assert _run(outreach.poll_replies(vid, force=True)) == 0
    assert pmf.read_metric(vid, 'outbound_touches') == 1
    assert pmf.read_metric(vid, 'outbound_replies') == 0


def test_unconfirmed_gmail_ack_is_held_and_never_counted_or_replayed(mail, monkeypatch):
    _, vid, sent = mail
    from app.autopilot import outreach, pmf
    _contacts(vid, 1)
    _run(outreach.draft_batch(vid))
    calls = []
    def no_ack(*args):
        calls.append(1)
        return {'provider_message_id': None, 'thread_id': None}
    monkeypatch.setattr(outreach, '_send_one', no_ack)
    first = _run(outreach.send_batch(vid, {'approved_by_founder': True}))
    assert first['kind'] == 'needs_you'
    assert outreach.drafts(vid)[0]['state'] == 'delivery_unconfirmed'
    assert outreach.drafts(vid)[0]['delivery_reserved_at']
    assert outreach.send_slots_used_today(vid) == 1
    assert pmf.read_metric(vid, 'outbound_touches') == 0
    _run(outreach.send_batch(vid, {'approved_by_founder': True}))
    assert calls == [1] and sent == []


def test_provider_delivery_then_timeout_is_unknown_and_reserves_daily_capacity(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach, pmf

    _on(client, vid, allow_send=True, send_max_per_day=1)
    _contacts(vid, 2)
    _run(outreach.draft_batch(vid, n=1, review_only=True))
    review_batch = outreach.drafts(vid, "draft")[0]["batch_id"]
    _run(outreach.draft_batch(vid, n=1))  # Another ordinary batch must not spend the held capacity.
    delivered = []

    def delivered_then_timeout(_box, to, _subject, _body, _thread):
        delivered.append(to)
        raise TimeoutError("private provider transport details")

    monkeypatch.setattr(outreach, "_send_one", delivered_then_timeout)
    reply = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": review_batch}))
    assert reply["kind"] == "needs_you" and "may have delivered" in reply["body_md"]
    assert "nothing went out" not in reply["body_md"].lower() and "private provider" not in str(reply)
    held = outreach.drafts(vid, "delivery_unconfirmed")
    assert len(held) == 1 and held[0]["delivery_reserved_at"] and held[0]["error"] == "gmail_delivery_unconfirmed"
    assert outreach.sends_today(vid) == 0 and outreach.send_slots_used_today(vid) == 1
    assert pmf.read_metric(vid, "outbound_touches") == 0
    _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": review_batch}))
    automatic = _run(outreach.send_batch(vid, {}))
    assert automatic["kind"] == "wait" and "used or reserved" in automatic["body_md"]
    assert len(delivered) == 1 and not sent and len(outreach.drafts(vid, "draft")) == 1


def test_process_failure_after_provider_delivery_leaves_a_non_replayable_reservation(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    class OwnedProcessFailure(BaseException):
        pass

    _on(client, vid, allow_send=True, send_max_per_day=1)
    _contacts(vid, 1)
    _run(outreach.draft_batch(vid, review_only=True))
    batch = outreach.drafts(vid, "draft")[0]["batch_id"]
    delivered = []

    def interrupted(_box, to, _subject, _body, _thread):
        delivered.append(to)
        raise OwnedProcessFailure("owned termination after delivery")

    monkeypatch.setattr(outreach, "_send_one", interrupted)
    with pytest.raises(OwnedProcessFailure):
        _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": batch}))
    held = outreach.drafts(vid, "delivery_pending")
    assert len(held) == 1 and held[0]["delivery_reserved_at"]
    assert outreach.send_slots_used_today(vid) == 1 and outreach.sends_today(vid) == 0
    retry = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": batch}))
    assert retry["kind"] == "needs_you" and "unconfirmed" in retry["body_md"]
    assert len(delivered) == 1 and not sent


@pytest.mark.parametrize("change", [{"allow_send": False}, {"allow_send": True, "send_max_per_day": 1}])
def test_automatic_batch_rereads_stop_or_lowered_cap_before_next_delivery(mail, monkeypatch, change):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True, send_max_per_day=3)
    _contacts(vid, 3)
    _run(outreach.draft_batch(vid))
    delivered = []

    def first_then_change(_box, to, _subject, _body, _thread):
        delivered.append(to)
        assert len(delivered) == 1, "the saved permission/cap change must prevent further provider I/O"
        response = client.put(f"/ventures/{vid}/autopilot", json=change)
        assert response.status_code == 200, response.text
        return {"provider_message_id": "owned-before-change", "thread_id": "owned-thread"}

    monkeypatch.setattr(outreach, "_send_one", first_then_change)
    reply = _run(outreach.send_batch(vid, {}))
    assert reply["kind"] == "wait" and "Confirmed 1" in reply["body_md"]
    assert "remaining drafts stay saved" in reply["body_md"]
    assert len(delivered) == 1 and not sent
    assert outreach.sends_today(vid) == 1 and outreach.send_slots_used_today(vid) == 1
    assert len(outreach.drafts(vid, "draft")) == 2 and not outreach.drafts(vid, "delivery_pending")


def test_exact_manual_batch_approval_remains_authorized_when_automatic_sending_is_off(mail, monkeypatch):
    client, vid, sent = mail
    from app.autopilot import outreach

    _on(client, vid, allow_send=True, send_max_per_day=3)
    _contacts(vid, 2)
    _run(outreach.draft_batch(vid, review_only=True))
    batch = outreach.drafts(vid, "draft")[0]["batch_id"]
    delivered = []

    def approved_send(_box, to, _subject, _body, _thread):
        delivered.append(to)
        if len(delivered) == 1:
            response = client.put(f"/ventures/{vid}/autopilot", json={"allow_send": False})
            assert response.status_code == 200, response.text
        return {"provider_message_id": f"owned-manual-{len(delivered)}", "thread_id": "owned-thread"}

    monkeypatch.setattr(outreach, "_send_one", approved_send)
    reply = _run(outreach.send_batch(vid, {"approved_by_founder": True, "batch_id": batch}))
    assert "Sent 2 outreach" in reply["body_md"] and len(delivered) == 2 and not sent
    assert len(outreach.drafts(vid, "sent")) == 2 and not outreach.drafts(vid, "draft")


# ---- PR-1: the draft carries the live page and a concrete offer, never an invented link ----

def test_draft_contains_the_live_page_url_and_a_concrete_offer(mail):
    _, vid, _ = mail
    from app.autopilot import outreach

    _contacts(vid, 2)
    _run(outreach.draft_batch(vid, n=2))
    for d in outreach.drafts(vid, "draft"):
        assert "https://gym.example/" in d["body"] and "early access" in d["body"]
        assert "15 minutes" in d["body"]


def test_draft_without_a_live_page_invents_no_url(mail, monkeypatch):
    _, vid, _ = mail
    from app.autopilot import outreach
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    monkeypatch.setattr(outreach, "site_url", lambda _v: None)
    _contacts(vid, 2)
    _run(outreach.draft_batch(vid, n=2))
    for d in outreach.drafts(vid, "draft"):
        contact = next(contact for contact in outreach.contacts(vid) if contact.id == d["contact_id"])
        source = json.loads(contact.enrichment_json)["found_at"]
        assert d["source_url"] == source and "Source:" not in d["body"]
        link = _opt_out_line(contact.workspace_id, contact.id, contact.email)
        assert d["body"].count(link) == 1
        assert "http" not in d["body"].replace(link, "") and "early access" not in d["body"]


def test_model_prompt_asks_for_a_concrete_offer_and_no_traction(mail, monkeypatch):
    _, vid, _ = mail
    from app.autopilot import llm, outreach

    seen = {}

    async def capture(venture_id, purpose, system, prompt, schema, max_tokens):
        seen["system"] = system

    monkeypatch.setattr(llm, "ask_json", capture)
    _run(outreach._llm_drafts(vid, [{"to": "a@b.example", "business": "B", "found": ""}], ""))
    assert "15-minute call" in seen["system"] and "early access" in seen["system"] and "never claim traction" in seen["system"]


@pytest.mark.parametrize("who", ["warehouse purchasing teams in Germany", "weekend hikers in Canada"])
def test_audience_binds_the_venture_scope_for_background_callers(monkeypatch, who):
    from app import tenancy
    from app.autopilot import outreach
    from app.gtm import journey

    seen = []
    monkeypatch.setattr(outreach, "_venture", lambda vid: ("ws_bg", ""))
    def icp(vid):
        scope = tenancy.current()  # raises ScopeRequired when unbound, like hosted working memory
        seen.append((scope.workspace_id, scope.venture_id))
        return {"who": who}
    monkeypatch.setattr(journey, "_icp", icp)
    monkeypatch.setattr(journey, "_row", lambda session, vid: None)
    monkeypatch.setattr(journey, "_journey_icp", lambda vid, saved, base=None: base or {})
    assert tenancy.current_or_none() is None
    assert who in outreach._audience("ven_bg")
    assert seen == [("ws_bg", "ven_bg")] and tenancy.current_or_none() is None


def test_missing_venture_has_no_audience_and_cannot_authorize_outreach(env):
    from app import tenancy
    from app.autopilot import outreach

    assert outreach._audience("ven_missing") == ""
    with tenancy.bind(workspace_id=_ws(env[1]), venture_id="ven_missing", actor="system"):
        assert outreach._audience("ven_missing") == ""
    with pytest.raises(KeyError, match="venture"):
        outreach._venture("ven_missing")

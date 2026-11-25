"""Channel and referral experiments work for unrelated business and consumer audiences."""
from datetime import timedelta
import asyncio
from html import unescape
import json
import re
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_page_experiments import consent_payload, record


@pytest.mark.parametrize("domain", ("freight-analytics", "family-cooking"))
def test_local_preview_links_allow_only_loopback_http_targets(domain, monkeypatch):
    from core.static_qa_contracts import Action

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for host in ("localhost", "127.0.0.1", "[::1]"):
        target = f"http://{host}:8000/public/waitlists/{domain}"
        assert Action(id="join", kind="external_link", role="link", name="Join early access", target=target).target == target
    for target in (f"http://{domain}.example/join", "http://localhost/join", "http://user:password@localhost/join",
                   f"http://localhost:0/public/waitlists/{domain}",
                   f"http://localhost:invalid/public/waitlists/{domain}", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            Action(id="join", kind="external_link", role="link", name="Join early access", target=target)


def test_local_signup_counts_require_consent_or_owner_totals(market):
    from app.product_analytics.service import measurement_counts
    from core.clock import utcnow

    now = utcnow()
    host = market[3]["icp"]["who"].lower().replace(" ", "-") + ".example"
    spec = {"id": market[1], "scan": "channel_test", "channel_kind": "channels", "site_url": f"https://{host}/", "max_duration_days": 14,
        "variants": [{"key": "email", "medium": "email"}]}
    context = {"prepared_at": (now - timedelta(days=1)).isoformat()}
    props = {"path": "/", "utm_campaign": spec["id"], "utm_source": "email", "utm_medium": "email"}
    events = [SimpleNamespace(name="signup", props=json.dumps(props), ts=now)]
    counts, sources, _views = measurement_counts(spec, {}, events, [])
    assert counts["email"]["signups"] == 0 and sources == []
    entry = SimpleNamespace(props=json.dumps(props), consent_text="I agree", created_at=now)
    counts, sources, _views = measurement_counts(spec, {}, events, [entry])
    assert counts["email"]["signups"] == 1 and sources == ["consented_waitlist"]
    manual = {"experiment_id": spec["id"], "channel": "email", "path": "/",
        "start_date": now.date().isoformat(), "end_date": now.date().isoformat(),
        "views": 5, "clicks": 2, "signups": 2}
    events.append(SimpleNamespace(name="manual_counts", props=json.dumps(manual), ts=now))
    with pytest.raises(ValueError, match="Open Experiments"):
        measurement_counts(spec, {}, events, [entry])
    counts, sources, _views = measurement_counts(spec, context, events, [entry])
    assert counts["email"] == {"views": 5, "clicks": 2, "signups": 2} and sources == ["manual_counts"]


@pytest.mark.parametrize("kind", ["channels", "referral"])
def test_local_manual_counts_feed_channel_readouts(market, monkeypatch, kind):
    from core.clock import utcnow
    from app.product_analytics import service
    from app.experiments import channel_tests
    from tests.unit.test_page_experiments import local_measurement_spec

    spec = local_measurement_spec(market, monkeypatch, kind)
    missing = channel_tests.observe(spec)
    assert missing["outcome"] == "inconclusive" and "Record views" in missing["insight"]
    payload = {"experiment_id": spec["id"], "path": "/", "start_date": utcnow().date().isoformat(),
        "end_date": utcnow().date().isoformat(), "channel": "alpha", "views": 100, "clicks": 20, "signups": 10}
    service.record_counts(market[1], payload)
    if kind == "channels":
        assert channel_tests.observe(spec)["outcome"] == "inconclusive"
    service.record_counts(market[1], {**payload, "channel": "beta", "signups": 5})
    result = channel_tests.observe(spec)
    assert result["measurement_sources"] == ["manual_counts"]
    assert "your manual counts" in result["insight"]
    assert result["measured"][spec["metric"]] == (10 / 15 if kind == "channels" else 1)
    if kind == "channels":
        assert [item["views"] for item in result["by_variant"]] == [100, 100]
        assert result["measured"]["clicks"] == 40
    else:
        assert result["measured"]["referred_signups"] == 15
    reply = channel_tests.readout(market[1], {"experiment_id": spec["id"]})
    prefill = next(action for action in reply["actions"] if action["action"] == "chat.prefill")
    assert json.loads(prefill["payload"]["text"].split(":", 1)[1])["channel"] == "alpha"
    if kind == "referral":
        from app.db.supymem_models import Experiment
        from app.state import db
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            context = json.loads(row.context)
            row.context = json.dumps({**context, "cohort": {}})
            session.commit()
        with pytest.raises(ValueError, match="consented signup on Site > Waitlist"):
            service.record_counts(market[1], payload)
    monkeypatch.setattr(service, "enabled", lambda name: False)
    assert channel_tests.readout(market[1], {"experiment_id": spec["id"]})["kind"] == "text"


def configured(market, kind, monkeypatch, **extra):
    from app.chat import experiments
    from app.experiments import channel_tests
    from app.approvals import external
    market[4].update(enabled=True, published_site={"url": "https://published.example/"})
    monkeypatch.setattr(external, "outbound_quote", lambda *_args: {"credit_estimate_micro": 7})
    payload = {"kind": kind, **extra}
    reply = asyncio.run(experiments.execute(market[1], "experiment.channel.prepare", payload, None))
    card = reply["actions"][0]["confirmation"]
    assert "Minimum sample:" in " ".join(card["consequences"])
    assert "credit" not in " ".join(card["consequences"]).lower()
    assert "Nothing is sent" in " ".join(card["consequences"])
    with pytest.raises(ValueError, match="confirmation_changed"):
        channel_tests.configure(market[1], {**card["resulting_state"], "fingerprint": "changed"})
    configured_reply = asyncio.run(experiments.execute(market[1], "experiment.channel.configure", card["resulting_state"], None))
    spec = channel_tests.available({"venture_id": market[1], "workspace_id": market[2],
        "direction": channel_tests._spec(market[1], payload)["direction"]})[-1]
    return spec, configured_reply


def test_disabled_builder_offers_a_page_free_interview_without_loop(market, monkeypatch):
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.chat import next_moves
    from app.chat.execute import execute_action
    from app.experiments import channel_tests
    from app.org.manager import capability_block
    from app.pagebuilder import api
    monkeypatch.setattr(api, "enabled", lambda: False)
    for kind in ("channels", "referral"):
        reply = channel_tests.prepare(market[1], {"kind": kind})
        assert "interview" in reply["body_md"].lower()
        assert not any(action.get("action") == "pagebuilder.build" for action in reply["actions"])
    if is_local():
        update_settings("capabilities", {"landing_pages": False}, {})
    reply = asyncio.run(execute_action(market[1], "pagebuilder.build", payload={"prepare": True}))
    if is_local():
        assert reply["body_md"] == blocked_message("landing_pages")
    else:
        assert "interview" in reply["body_md"].lower()
    assert not any(action.get("action") == "experiment.list" for action in reply["actions"])
    assert "landing page builder is unavailable" in capability_block().lower()
    assert not any(action.get("action") == "pagebuilder.build" for action in next_moves.compose(market[1])["actions"])


def test_channel_page_prerequisite_keeps_the_page_target_when_disabled(market, monkeypatch):
    from app.core.local_capabilities import blocked_message
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from fastapi import HTTPException
    from app.chat.execute import execute_action
    from app.db.models import Job
    from app.experiments import channel_tests, ledger
    from app.pagebuilder import api
    from app.state import db

    venture_id = market[1]
    original = ledger.list_for_venture(venture_id)
    with db()() as session:
        jobs_before = set(session.scalars(select(Job.id).where(Job.venture_id == venture_id)))
    for kind in ("channels", "referral"):
        monkeypatch.setattr(api, "enabled", lambda: True)
        reply = channel_tests.prepare(venture_id, {"kind": kind})
        page = reply["actions"][0]
        assert page["action"] == "pagebuilder.build" and page["payload"]["prepare"] is True
        monkeypatch.setattr(api, "enabled", lambda: False)
        if is_local():
            update_settings("capabilities", {"landing_pages": False}, {})
        unavailable = asyncio.run(execute_action(venture_id, page["action"], payload=page["payload"]))
        if is_local():
            assert unavailable["body_md"] == blocked_message("landing_pages")
        else:
            assert "landing page" in unavailable["body_md"].lower() and "not available" in unavailable["body_md"].lower()
            assert unavailable["actions"][0]["action"] == "chat.prefill" and "interview" in unavailable["body_md"]
        assert not any(action.get("confirmation") or action.get("action", "").startswith(("build.", "plan.", "research."))
            for action in unavailable["actions"])
        monkeypatch.setattr(api, "enabled", lambda: True)
        if is_local():
            update_settings("capabilities", {"landing_pages": True}, {})

        def guarded(*args):
            raise HTTPException(409, detail={"code": "pagebuilder_no_active_prices"})

        with monkeypatch.context() as enabled:
            enabled.setattr(api, "estimate", guarded)
            actor = None
            if is_local():
                from app import coding_agents_consent
                from app.core.local_owner import ensure_owner
                from desktop_runtime import agent_status
                actor = ensure_owner().id
                enabled.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
                enabled.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
                    "signed_in": True, "subscription_login": True, "available": True}])
            with pytest.raises(HTTPException) as denied:
                asyncio.run(execute_action(venture_id, page["action"], payload=page["payload"], actor_subject=actor))
        assert denied.value.detail == {"code": "pagebuilder_no_active_prices"}
    assert ledger.list_for_venture(venture_id) == original
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.venture_id == venture_id))) == jobs_before
    assert not market[6]


def test_channel_links_drafts_attribution_and_thresholds(market, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import Approval, GtmContact, GtmOutreachSettings, VentureDocument
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, channel_tests
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.state import db
    from core.clock import utcnow
    from datetime import timedelta
    from app.credits.units import MICRO_PER_CREDIT
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    venture_id, workspace_id = market[1:3]
    monkeypatch.setattr("app.chat.actions.social_post_enabled", lambda: True)
    with db()() as session:
        session.add(GtmContact(id="channel_contact", workspace_id=workspace_id, email="reader@interest-mail.example",
            source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://address-source.example/contact"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=workspace_id, created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "42 Cedar Street, Springfield, US"
        if is_local():
            update_settings("smtp", {"host": "smtp.example.invalid", "user": "you@example.invalid",
                "sender_name": "You", "postal_address": settings.sender_postal_address}, {"password": "test-only-password"})
            update_settings("capabilities", {"send_email": True}, {})
        else:
            monkeypatch.setenv("SUPYMEM_SENDER_POSTAL_ADDRESS", settings.sender_postal_address)
        session.commit()
    outreach._save_draft(venture_id, {"contact_id": "channel_contact", "to": "reader@interest-mail.example",
        "day": 1, "channel": "email", "state": "draft", "batch_id": "existing_review_batch", "review_state": "draft",
        "requires_journey_review": True, "subject": "A quick question", "body": "How do you handle this problem?"})
    communities = [{"title": 'Local makers & "friends"', "url": "https://forum.example/groups/a?topic=tools&mode=public"},
        {"title": "Shared interests", "url": "https://club.example/people", "participation": "No self promotion"}]
    spec, reply = configured(market, "channels", monkeypatch, communities=communities)
    assert spec["catalog_key"].startswith("E25")
    assert spec["id"] in {item["id"] for item in autonomous.available(venture_id)}
    with db()() as session:
        context = json.loads(session.get(Experiment, spec["id"]).context)
    assert len(context["links"]) == 5 and len(set(context["links"].values())) == 5
    for variant in spec["variants"]:
        query = parse_qs(urlsplit(context["links"][variant["key"]]).query)
        assert query == {"utm_source": [variant["key"]], "utm_medium": [variant["medium"]], "utm_campaign": [spec["id"]]}
    escaped = channel_tests.tagged_link('https://published.example/a?keep=1&keep=2&utm_source=old#part',
        utm_source='readers & "writers"', utm_medium="community", utm_campaign=spec["id"])
    assert parse_qs(urlsplit(escaped).query)["keep"] == ["1", "2"]
    assert parse_qs(urlsplit(escaped).query)["utm_source"] == ['readers & "writers"']
    assert urlsplit(escaped).fragment == "part"
    with pytest.raises(ValueError):
        channel_tests.tagged_link("javascript:alert(1)", utm_source="other")
    draft = outreach.drafts(venture_id)[0]
    assert context["links"]["email"] in draft["body"] and draft["requires_batch_approval"] and draft["requires_journey_review"]
    assert OPT_OUT in draft["body"] and "42 Cedar Street" in draft["body"]
    assert "Your email address is listed publicly on address-source.example." in draft["body"]
    from app.gtm import journey, playbooks
    assert not playbooks.copy_problem(draft["body"])
    assert playbooks.copy_problem("What have you tried? What still fails? " + context["links"]["email"])
    monkeypatch.setattr(journey, "authority", lambda *_args, **_kwargs: "local")
    journey.patch_draft(workspace_id, venture_id, draft["doc_id"], {"expected_revision": journey._draft_revision(draft), "decision": "approved"})
    assert outreach.drafts(venture_id)[0]["review_state"] == "approved"
    assert "Your X draft:" in reply["body_md"] and context["links"]["x"] in reply["body_md"]
    social = next(action["confirmation"] for action in reply["actions"] if action.get("confirmation", {}).get("action") == "social_post")
    assert social["resulting_state"]["platforms"] == ["twitter"]
    assert context["links"]["x"] in social["resulting_state"]["content"]
    assert "Do not post your link" in reply["body_md"]
    for variant in spec["variants"]:
        props = {"utm_source": variant["key"], "utm_medium": variant["medium"], "utm_campaign": spec["id"]}
        record(spec, "landing_view", 10, path="/", **props)
        record(spec, "landing_view", 3, path=f"/public/waitlists/{venture_id}", **props)
        record(spec, "landing_view", 2, path="/", **{**props, "utm_campaign": "different"})
        record(spec, "signup", 100, **props)
    from app.conversion import waitlist
    for index in range(9):
        waitlist.capture(venture_id, consent_payload(venture_id, f"signup{index}@interest-mail.example",
            utm_source="email", utm_medium="email", utm_campaign=spec["id"]))
    assert channel_tests.observe(spec)["outcome"] == "inconclusive"
    waitlist.capture(venture_id, consent_payload(venture_id, "tenth@interest-mail.example",
        utm_source="x", utm_medium="social", utm_campaign=spec["id"]))
    waitlist.capture(venture_id, consent_payload(venture_id, "wrong@interest-mail.example",
        utm_source="email", utm_medium="social", utm_campaign=spec["id"]))
    measured = channel_tests.observe(spec)
    assert measured["outcome"] == "supported" and measured["measured"]["events"] == 10
    assert "None" not in measured["insight"] and measured["insight"].count("Next:") == 1
    assert [(item["views"], item["signups"]) for item in measured["by_variant"]] == [(10, 9), (10, 0), (10, 0), (10, 1), (10, 0)]
    with db()() as session:
        document = session.get(VentureDocument, draft["doc_id"])
        saved = json.loads(document.provenance_json)
        document.provenance_json = json.dumps({**saved, "state": "sent", "delivery_approval_id": "settled_email"})
        session.add(Approval(id="settled_email", workspace_id=workspace_id, kind="external_comms", status="approved",
            subject_ref=draft["doc_id"], summary="Saved send", requested_by="founder", created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=1), detail_json=json.dumps({"external_action": "gtm_email_send",
                "external_execution": {"state": "succeeded", "settled_micro": 2 * MICRO_PER_CREDIT}})))
        session.commit()
    assert channel_tests.observe(spec)["by_variant"][0]["signups_per_credit"] == 4.5
    channel_tests.log_effort(venture_id, {"experiment_id": spec["id"], "channel": "email", "founder_minutes": 30, "credits": 3})
    email = channel_tests.observe(spec)["by_variant"][0]
    assert email["signups_per_founder_hour"] == 18 and email["signups_per_credit"] == 3
    with pytest.raises(ValueError, match="effort_invalid"):
        channel_tests.log_effort(venture_id, {"experiment_id": spec["id"], "channel": "email", "credits": float("nan")})


def test_referral_codes_capture_consent_footer_and_readout(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.autopilot import outreach
    from app.conversion import waitlist
    from app.db.models import GtmContact, GtmOutreachSettings, GtmSuppression
    from app.db.supymem_models import Experiment, VentureWaitlistEntry
    from app.experiments import channel_tests, outreach_tests, page_tests
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    from app.state import db
    from core.clock import utcnow
    venture_id, workspace_id = market[1:3]
    for index in range(30):
        waitlist.capture(venture_id, consent_payload(venture_id, f"member{index}@interest-mail.example"))
    with db()() as session:
        initial_codes = {entry.email: json.loads(entry.props)["ref_code"] for entry in session.scalars(select(VentureWaitlistEntry))}
        assert len(set(initial_codes.values())) == 30
        session.add_all([VentureWaitlistEntry(id="test_entry", venture_id=venture_id, workspace_id=workspace_id,
            email="probe@qa.invalid", is_test=True, consent_version=waitlist.VERSION, consent_text="Recorded probe", created_at=utcnow()),
            VentureWaitlistEntry(id="no_consent", venture_id=venture_id, workspace_id=workspace_id,
            email="no-consent@interest-mail.example", is_test=False, consent_version=waitlist.VERSION, consent_text="", created_at=utcnow()),
            GtmSuppression(id="referral_optout", workspace_id=workspace_id, email="member29@interest-mail.example",
                reason="opt_out", source="opt_out", created_at=utcnow())])
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=workspace_id, created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "42 Cedar Street, Springfield, US"
        if is_local():
            update_settings("smtp", {"host": "smtp.example.invalid", "user": "you@example.invalid",
                "sender_name": "You", "postal_address": settings.sender_postal_address}, {"password": "test-only-password"})
            update_settings("capabilities", {"send_email": True}, {})
        else:
            monkeypatch.setenv("SUPYMEM_SENDER_POSTAL_ADDRESS", settings.sender_postal_address)
        session.commit()
    spec, reply = configured(market, "referral", monkeypatch)
    drafts = outreach.drafts(venture_id)
    assert len(drafts) == 29 and spec["catalog_key"].startswith("E27")
    assert all(item["one_off"] and item["requires_batch_approval"] and item["referral_ask"] for item in drafts)
    assert all(OPT_OUT in item["body"] and "42 Cedar Street" in item["body"] and
        "I have your address from my own contacts." in item["body"] for item in drafts)
    assert all("no-consent" not in item["to"] and "probe" not in item["to"] for item in drafts)
    assert channel_tests.observe(spec)["outcome"] == "inconclusive"
    with db()() as session:
        entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.email.like("member%"))).all()
        codes = {entry.email: channel_tests.referral_code(entry) for entry in entries}
        assert codes == initial_codes
        assert len(set(codes.values())) == 30
        assert all(channel_tests.referral_code(entry) == codes[entry.email] and entry.email not in codes[entry.email] for entry in entries)
        subject, body = channel_tests.referral_mail(entries[0], {**spec, "product_name": spec["product_name"] + " \u2014 next version"})
        assert "\u2014" not in subject + body
        assert json.loads(session.get(VentureWaitlistEntry, "test_entry").props or "{}") == {}
        session.commit()
    assert page_tests.draft_mail(venture_id, spec["id"], referral=True)["count"] == 29
    assert len(outreach.drafts(venture_id)) == 29
    for draft in drafts:
        link = re.search(r"https://published\.example/\?ref=\S+", draft["body"]).group()
        assert parse_qs(urlsplit(link).query)["ref"] == [codes[draft["to"]]]
    assert outreach_tests.send_block(venture_id, drafts[0]) is None
    with db()() as session:
        contact = session.get(GtmContact, drafts[0]["contact_id"])
        original_email = contact.email
        contact.email = "changed_recipient@interest-mail.example"
        session.commit()
    assert outreach_tests.send_block(venture_id, drafts[0]) == "experiment_contact_ineligible"
    with db()() as session:
        session.get(GtmContact, drafts[0]["contact_id"]).email = original_email
        session.commit()
    assert outreach_tests.send_block(venture_id, {**drafts[0], "body": "changed"}) == "experiment_mail_changed"
    assert outreach_tests.send_block(venture_id, {**drafts[0], "credit_ceiling_micro": 14}) == "experiment_mail_changed"
    edited = {**drafts[0], "body": drafts[0]["body"] + "\n\nThank you.", "batch_id": "reviewed_referral_edit"}
    outreach._save_draft(venture_id, edited, edited["doc_id"])
    assert outreach_tests.send_block(venture_id, edited) is None
    drafts[0] = edited
    waitlist.capture(venture_id, consent_payload(venture_id, "member0@interest-mail.example", ref=codes["member0@interest-mail.example"]))
    waitlist.capture(venture_id, consent_payload(venture_id, "unknown@interest-mail.example", ref="unknown_code"))
    for index in range(3):
        waitlist.capture(venture_id, consent_payload(venture_id, f"friend{index}@interest-mail.example", ref=codes[f"member{index}@interest-mail.example"]))
    waitlist.capture(venture_id, consent_payload(venture_id, "another@interest-mail.example", ref=codes["member0@interest-mail.example"]))
    with db()() as session:
        entries = {entry.email: json.loads(entry.props or "{}") for entry in session.scalars(select(VentureWaitlistEntry))}
    assert "referred_by" not in entries["member0@interest-mail.example"] and "referred_by" not in entries["unknown@interest-mail.example"]
    assert entries["friend0@interest-mail.example"]["referred_by"] == codes["member0@interest-mail.example"]
    assert entries["friend0@interest-mail.example"]["ref_code"] not in set(codes.values())
    result = channel_tests.observe(spec)
    expected = {"events": 30, "referrers": 3, "referred_signups": 4, "referrer_share": .1}
    if is_local():
        expected.update(views=0, clicks=0)
    assert result["outcome"] == "supported" and result["measured"] == expected
    assert "3/30" in result["insight"] and "10%" in result["insight"]
    assert "Next:" in result["insight"]
    sent, meters = [], []
    monkeypatch.setattr("app.approvals.external.outbound_rate", lambda *_args: SimpleNamespace(amount_micro=7))
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: sent.append(_args) or {"provider_message_id": "real_message"})
    def meter(invoke, **kwargs):
        meters.append(kwargs)
        receipt = invoke()
        kwargs["before_meter_exit"](receipt, {"actual_micro": 7})
        return receipt
    monkeypatch.setattr("app.credits.metered.call_sync", meter)
    outreach._save_draft(venture_id, {**drafts[0], "state": "delivery_pending"}, drafts[0]["doc_id"])
    assert page_tests.send_one(venture_id, workspace_id, drafts[0], {})["provider_message_id"] == "real_message"
    assert len(sent) == 1 and meters[0]["purpose"] == "experiment.waitlist_mail"
    with pytest.raises(DefiniteFailure, match="no longer eligible"):
        page_tests.send_one(venture_id, workspace_id, drafts[0], {})
    assert len(sent) == 1
    with db()() as session:
        entry = session.get(VentureWaitlistEntry, drafts[1]["waitlist_entry_id"])
        entry.consent_text = ""
        session.commit()
    with pytest.raises(DefiniteFailure, match="consent"):
        page_tests.send_one(venture_id, workspace_id, drafts[1], {})
    assert len(sent) == 1
    assert any(action["action"] == "outreach.send" for action in reply["actions"])


def test_channel_and_referral_attribution_cross_the_public_form_without_beacon_personal_data(market, monkeypatch):
    from app.conversion import waitlist
    from app.core.local_mode import is_local
    from app.product_analytics.beacon import script_tag
    venture_id = market[1]
    attrs = {"utm_source": "community_abc", "utm_medium": "community", "utm_campaign": "exp_abc", "ref": "random_personal_code"}
    response = market[0].get(f"/public/waitlists/{venture_id}", params=attrs)
    if is_local():
        from app.db.supymem_models import VentureWaitlistEntry
        from app.state import db

        assert response.status_code == 404 and "Coding tools" in response.text and "<form" not in response.text
        captured = market[0].post(f"/public/waitlists/{venture_id}",
            json=consent_payload(venture_id, "reader@interest-mail.example", **attrs))
        assert captured.status_code == 201, captured.text
        with db()() as session:
            entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id))
            stored = json.loads(entry.props)
            assert all(stored[key] == value for key, value in attrs.items() if key != "ref")
            assert "ref" not in stored
        from urllib.parse import urlencode
        from app.experiments import page_tests

        def checked_waitlist(checked_venture, experiment_id, filename, attribution=None):
            assert (checked_venture, experiment_id, filename) == (venture_id, "", "waitlist.html")
            return "https://owned-site.example/waitlist.html?" + urlencode(attribution)

        monkeypatch.setattr(page_tests, "checked_public_url", checked_waitlist)
        redirected = market[0].get(f"/public/waitlists/{venture_id}", params=attrs, follow_redirects=False)
        assert redirected.status_code == 303
        assert parse_qs(urlsplit(redirected.headers["location"]).query) == {key: [value] for key, value in attrs.items()}
        assert "<form" not in redirected.text
    else:
        assert response.status_code == 200
        for key, value in attrs.items():
            assert f'name="{key}" value="{value}"' in unescape(response.text)
    assert market[0].get(f"/public/waitlists/{venture_id}", params={"ref": '\"><script>'}).status_code == 422
    with pytest.raises(waitlist.WaitlistError):
        waitlist.capture(venture_id, consent_payload(venture_id, "reader@interest-mail.example", ref=[]))
    with monkeypatch.context() as hosted:
        hosted.delenv("SUPYMEM_MODE", raising=False)
        script = script_tag(key="pk_app_test", endpoint="https://gateway.example/e").split('data-supymem-analytics="1">', 1)[1].rsplit("</script>", 1)[0]
    target = f"https://gateway.example/public/waitlists/{venture_id}"
    harness = """
const vm = require('node:vm');
const emitted = []; let listener;
const location = new URL('https://published.example/?utm_source=community_abc&utm_medium=community&utm_campaign=exp_abc&ref=random_personal_code');
const link = {href: TARGET, getAttribute: () => null};
const document = {referrer: '', querySelectorAll: () => [], querySelector: () => null, addEventListener: (_event, handler) => {listener = handler;}};
const navigator = {sendBeacon: (_url, body) => {emitted.push(body);}};
vm.runInNewContext(SCRIPT, {location, document, navigator, window: {}, URL, URLSearchParams, Blob});
listener({target: {closest: () => link}});
Promise.all(emitted.map(async body => JSON.parse(await body.text()))).then(events => console.log(JSON.stringify({events, href: link.href})));
""".replace("TARGET", json.dumps(target)).replace("SCRIPT", json.dumps(script))
    executed = subprocess.run(["node", "-e", harness],
        capture_output=True, text=True, check=True)
    observed = json.loads(executed.stdout)
    assert parse_qs(urlsplit(observed["href"]).query) == {key: [value] for key, value in attrs.items()}
    assert len(observed["events"]) == 2
    assert all(event["props"].get("utm_source") == "community_abc" and "ref" not in event["props"] for event in observed["events"])

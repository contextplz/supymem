"""Chat-first: contact import and website review run as chat cards; no chat button links to a hidden page."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_journey import journey_db as journey_db  # noqa: F401 - fixture

load_company_os_app()
from tests.unit.chat.hidden_pages import _SEGMENTS, hidden_hrefs  # noqa: E402

_LOCAL_SCREENS = {"/connections?venture=ven_1", *(f"/v/ven_1/{segment}?x=1"
    for segment in ("builds", "connections", "experiments", "research"))}


@pytest.mark.parametrize("audience,company,domain", [
    ("Warehouse purchasing teams", "Cobalt Parts", "cobalt-parts.example"),
    ("Parents planning weekend outings", "Willow Canoes", "willow-canoes.example"),
])
@pytest.mark.parametrize("variant", ["period", "comma", "nameless", "invalid", "csv"])
def test_known_person_sentence_is_saved_without_import_language(journey_db, monkeypatch, audience, company, domain, variant):
    from app.autopilot import outreach
    from app.chat.router import handle_message
    from app.db.models import GtmCompany, GtmContact, Job, Venture, VentureDocument
    from app.gtm import journey
    from app.intake import contacts
    from app.state import db
    from sqlalchemy import select

    _mx(monkeypatch)
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: pytest.fail("Adding a person must not search"))
    address = f"ren+event@{domain}"
    note = "I met them at a conference."
    description = f"Ren Dalton, Buyer at {company}, {address}"
    if variant == "nameless":
        description = f"Buyer at {company}, {address}"
    if variant == "invalid":
        description = description.replace(address, f"ren@@{domain}")
    message = f"Add someone I know: {description}{',' if variant == 'comma' else '.'} {note}"
    if variant == "csv":
        message = f"Import these contacts:\nname,email,company,title,notes\nRen Dalton,{address},{company},Buyer,{note}"
    with db()() as session:
        session.get(Venture, journey_db["vid"]).raw_idea = audience
        session.commit()
        jobs = list(session.scalars(select(Job.id)))
    pending = asyncio.run(contacts.preview(journey_db["wid"], journey_db["vid"],
        f"name,email\nOther Person,other@{domain}", format="csv", mapping=None, actor=journey_db["actor"]))
    reply = asyncio.run(handle_message(journey_db["vid"], message, actor_subject=journey_db["actor"]))
    body = re.sub(r"<!--.*?-->\n?", "", reply["body_md"])
    assert reply["kind"] != "confirmation"
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        saved = session.scalar(select(GtmContact).where(GtmContact.email == address))
        assert list(session.scalars(select(Job.id))) == jobs
        if variant == "csv":
            assert "Contact preview" in reply["body_md"] and saved is None
        elif variant == "invalid":
            assert "email address" in body and "\n" not in body and saved is None
        else:
            assert saved is not None, reply
            assert saved.first_name == (None if variant == "nameless" else "Ren")
            assert saved.last_name == (None if variant == "nameless" else "Dalton")
            assert saved.role == "Buyer"
            assert session.get(GtmCompany, saved.company_id).canonical_name == company
            assert saved.source == f"venture:{journey_db['vid']}:founder_import"
            assert saved.contact_provenance == "founder_import" and saved.lawful_basis == "none"
            assert saved.lawful_basis_at is None
            evidence = json.loads(saved.enrichment_json)["founder_import"]
            assert evidence["notes"] == note and evidence["lawful_basis_attested"] is False
            assert journey.source_line(saved) == "I have your address from my own contacts."
            assert journey._block(session, journey_db["wid"], saved) is not None
            assert "Nothing was sent" in reply["body_md"] and "You can" in reply["body_md"]
        if variant != "csv":
            assert not any(word in reply["body_md"].lower() for word in ("columns", "rows", "preview", "credits"))
            preview = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == journey_db["vid"],
                VentureDocument.kind == contacts._preview_kind(journey_db["actor"])))
            assert json.loads(preview.provenance_json)["preview_id"] == pending["preview_id"]
    assert not outreach.drafts(journey_db["vid"])


@pytest.mark.parametrize("audience,address", [
    ("Industrial procurement teams", "lee@factory.example"),
    ("Family cycling groups", "jo@cycling.example"),
])
def test_conn_regression_requested_csv_uses_intake_not_outreach(journey_db, monkeypatch, audience, address):
    from app.chat.router import handle_message
    from app.chat.execute import execute_action
    from app.db.models import GtmContact, Venture
    from app.state import db
    from sqlalchemy import select

    _mx(monkeypatch)
    with db()() as session:
        session.get(Venture, journey_db["vid"]).raw_idea = audience
        session.commit()
    reply = asyncio.run(handle_message(journey_db["vid"], f"Import these contacts:\nname,email\nLee Rowan,{address}",
                                       actor_subject=journey_db["actor"]))
    assert "Contact preview" in reply["body_md"]
    assert reply["kind"] != "confirmation"
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        assert session.scalar(select(GtmContact).where(GtmContact.email == address)) is None
    permission = next(action for action in reply["actions"] if action.get("action") == "contacts.import")
    assert permission["payload"]["lawful_basis_attested"] is True
    done = asyncio.run(execute_action(journey_db["vid"], "contacts.import", payload=permission["payload"],
                                     actor_subject=journey_db["actor"]))
    assert "Imported **1** contact" in done["body_md"] and "Nothing was sent" in done["body_md"]


def test_hidden_segments_except_settings_map_to_an_in_chat_action():
    from app.chat.actions import REGISTRY
    from app.chat.navigation_map import in_chat
    from app.core.local_mode import is_local

    assert "connections" in _SEGMENTS and "site" in _SEGMENTS
    links = [{"label": s, "href": f"/v/ven_1/{s}?x=1"} for s in _SEGMENTS if s != "settings"] + [
        {"label": "g", "href": "/gtm?venture=ven_1&import=1"}, {"label": "o", "href": "/ops/outreach"},
        {"label": "c", "href": "/connections?venture=ven_1"}, {"label": "abs", "href": "https://app.supymem.com/v/ven_1/site"}]
    converted = in_chat(links)
    visible = [link["href"] for link in links if is_local() and link["href"] in _LOCAL_SCREENS]
    assert [a["href"] for a in converted if a.get("href")] == visible
    assert {a["action"] for a in converted if not a.get("href")} <= set(REGISTRY)
    secure = {"label": "Open Connections", "href": "/connections?venture=ven_1", "kind": "secret_form"}
    assert in_chat([secure]) == [secure]  # credential forms stay links
    assert in_chat([{"label": "Open live site", "href": "https://acme.example"}])[0]["href"] == "https://acme.example"


@pytest.mark.hosted_only
@pytest.mark.parametrize("domain", ["Equipment certification for factory teams", "Recipe sharing for home cooks"])
def test_unhandled_actions_are_disabled_and_a_bypassed_click_logs_an_error(journey_db, caplog, domain):
    import logging
    from app.chat.execute import _execute_action
    from app.chat.navigation_map import in_chat_reply
    from app.chat.store import append_message, list_messages

    vid = journey_db["vid"]
    actions = [{"label": "Unavailable step", "action": "unknown.next_step", "style": "primary", "kind": "unknown_form"},
               {"label": "Unavailable confirmation", "confirmation": {"action": "scope.request"}, "href": "/account"},
               {"label": "Show review", "action": "startup.review"},
               {"label": "Answer", "action": "chat.reply"},
               {"label": "Prepare on your Mac", "action": "mac.task.prepare"}]
    saved = append_message(vid, role="assistant", kind="navigation", body_md=domain, actions=actions)
    for reply in (saved, list_messages(vid)[-1], in_chat_reply({"body_md": domain, "actions": actions})):
        for action in reply["actions"][:2]:
            assert "cannot use this action" in action["disabled_reason"]
        assert all(not action.get("disabled_reason") for action in reply["actions"][2:])
    assert all("disabled_reason" not in action for action in actions)
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="unknown chat action"):
        asyncio.run(_execute_action(vid, "unknown.next_step"))
    with caplog.at_level(logging.ERROR):
        result = asyncio.run(_execute_action(vid, "scope.request"))
    assert result["action_started"] is False
    assert "cannot use this action" in result["body_md"]
    assert result["actions"] == [{"label": "Show your saved status", "action": "status", "style": "primary"}]
    assert sum(record.message.startswith("error_event ") and '"code": "unhandled_chat_action"' in record.message
               for record in caplog.records) == 2


@pytest.mark.parametrize("href", [f"/v/ven_1/{segment}?x=1" for segment in _SEGMENTS if segment != "settings"] + [
    "/gtm?venture=ven_1", "/ops/outreach", "/connections?venture=ven_1", "/v/ven_1/gtm",
    "/v/ven_1/site?build=1", "https://app.supymem.com/v/ven_1/site",
])
def test_hidden_body_links_use_the_shared_navigation_mapping(href):
    from app.chat.actions import disabled_reason
    from app.chat.navigation_map import hidden_href, in_chat_reply
    from app.core.local_mode import is_local

    target = hidden_href(href)
    if is_local() and href in _LOCAL_SCREENS:
        # 2d2d6dabd and ed24fe27b keep these local screens navigable.
        assert target is None
        for body in (f"Next: [Open screen]({href}).", f"Next: {href}.", f'Next: "{href}".'):
            reply = in_chat_reply({"body_md": body})
            assert reply["body_md"] == body and not reply.get("actions")
        return
    for body, expected in [(f"Next: [Review your drafts]({href}).", "Next: Review your drafts."),
                           (f"Next: {href}.", f"Next: {target['label']}."),
                           (f'Next: "{href}".', f'Next: "{target["label"]}".')]:
        reply = in_chat_reply({"body_md": body})
        assert reply["body_md"] == expected
        action = {**target, "style": "ghost"}
        if reason := disabled_reason(target["action"]):
            action["disabled_reason"] = reason
        assert reply["actions"] == [action]
        assert in_chat_reply(reply) == reply


def test_body_link_conversion_preserves_external_links_code_and_existing_cards():
    from app.chat.navigation_map import in_chat_reply

    untouched = ("[External](https://example.com/v/ven_1/research) https://example.com/v/ven_1/gtm. "
                 "[Idea](/v/ven_1/idea/current) ` /v/ven_1/research `\n"
                 "```markdown\n[Example](/v/ven_1/gtm)\n```\n"
                 "//evil.example/v/ven_1/research /other/v/ven_1/research javascript:/v/ven_1/research")
    existing = {"label": "Review drafts", "action": "gtm.status", "style": "primary"}
    original = {"body_md": untouched + "\n[Review your drafts](/v/ven_1/gtm)", "actions": [existing]}
    reply = in_chat_reply(original)
    assert reply["body_md"] == untouched + "\nReview your drafts"
    assert reply["actions"] == [existing]
    assert original["body_md"].endswith("[Review your drafts](/v/ven_1/gtm)")
    assert in_chat_reply({**original, "role": "user"}) == {**original, "role": "user"}
    assert in_chat_reply({**original, "kind": "memory_list"}) == {**original, "kind": "memory_list"}


def test_idea_journey_links_are_chat_actions_only_for_chat_first(monkeypatch):
    from app.chat.navigation_map import in_chat_reply
    from app.credits import policy
    link = {"label": "Full report", "href": "/v/ven_1/idea?chat=1", "style": "primary"}
    reply = {"kind": "navigation", "body_md": "[Full report](/v/ven_1/idea)", "actions": [link]}
    for layout in ("chat", "home", "classic"):
        monkeypatch.setattr(policy, "get_policy", lambda *_args: {"founder_ui": layout})
        served = in_chat_reply(reply)
        if layout == "classic":
            assert served == reply
        else:
            assert served["body_md"] == "Full report"
            assert served["actions"] == [{"label": "Show my idea review here", "action": "idea.review", "style": "primary"}]
            assert in_chat_reply(served) == served


def test_new_replayed_and_old_saved_assistant_body_links_are_served_in_chat(journey_db):
    from app.chat.store import _ser, append_message, idempotent_message, list_messages
    from app.core.local_mode import is_local
    from app.db.models import ChatMessage

    body = "[Review your drafts](/v/ven_1/gtm) or /v/ven_1/research."
    vid = journey_db["vid"]
    first = append_message(vid, role="assistant", body_md=body, kind="navigation", idempotency_key="body-links")
    replay = append_message(vid, role="assistant", body_md=body, kind="navigation", idempotency_key="body-links")
    saved = idempotent_message(vid, "assistant", "body-links")
    history = next(message for message in list_messages(vid) if message["id"] == first["id"])
    old = _ser(ChatMessage(id="old-links", venture_id=vid, role="assistant", kind="navigation", body_md=body))
    expected = "Review your drafts or /v/ven_1/research." if is_local() else "Review your drafts or Show research here."
    for reply in (first, replay, saved, history, old):
        assert expected in reply["body_md"]
        assert reply["content"] == expected
        assert reply["parts"] == [{"type": "text", "text": reply["content"]}]
        assert [action["action"] for action in reply["actions"]] == (["gtm.status"] if is_local() else ["gtm.status", "research"])
        assert not hidden_hrefs(reply)
    assert first["id"] == replay["id"] == saved["id"]
    founder = append_message(vid, role="user", body_md=body, kind="navigation")
    assert founder["body_md"] == body and not founder["actions"]


def _mx(monkeypatch):
    from app.intake import contacts

    async def mx(domains):
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", mx)


def test_pasted_contacts_preview_then_supply_permission_in_chat(journey_db, monkeypatch):
    from app.chat.execute import execute_action
    from app.intake.assets import chat_asset_reply

    d = journey_db
    _mx(monkeypatch)
    offer = chat_asset_reply(d["vid"], "Here are my contacts:\nAda Baker <ada@bakery.example>\nbob@bakery.example\nbad@@x")
    button = offer["actions"][0]
    assert button["label"] == "Import contacts" and "href" not in button
    preview = asyncio.run(execute_action(d["vid"], button["action"], payload=button["payload"], actor_subject=d["actor"]))
    assert preview["kind"] == "text" and not hidden_hrefs(preview)
    assert "**Contact preview**: 3 rows, **2 ready to import**" in preview["body_md"] and "Row 3:" in preview["body_md"]
    assert "1. Ada Baker, ada@bakery.example" in preview["body_md"]
    card = preview["actions"][1]
    assert card["label"] == "You have a legitimate interest" and card["action"] == "contacts.import"
    assert card["payload"]["lawful_basis_attested"] is True
    assert not any(action.get("confirmation") for action in preview["actions"])
    refused = asyncio.run(execute_action(d["vid"], "contacts.import", payload={"preview_id": card["payload"]["preview_id"]}, actor_subject=d["actor"]))
    assert "lawful basis" in refused["body_md"]
    done = asyncio.run(execute_action(d["vid"], "contacts.import", payload=card["payload"], actor_subject=d["actor"]))
    assert "Imported **2** contacts" in done["body_md"] and "Nothing was sent" in done["body_md"]
    assert done["actions"] == [{"label": "Show customers and drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}]
    from sqlalchemy import select
    from app.db.models import GtmContact
    from app.state import db
    with db()() as session:
        saved = session.scalars(select(GtmContact).where(GtmContact.workspace_id == d["wid"])).all()
        assert {c.email for c in saved} >= {"ada@bakery.example", "bob@bakery.example"}
        assert all(c.lawful_basis == "legitimate_interest" for c in saved if c.contact_provenance == "founder_import")
    replay = asyncio.run(execute_action(d["vid"], "contacts.import", payload=card["payload"], actor_subject=d["actor"]))
    assert "Imported **2**" in replay["body_md"]  # the same receipt; nothing imported twice


def test_attached_csv_preview_and_stale_preview_is_refused(journey_db, monkeypatch):
    from app.chat.execute import execute_action
    from app.intake.evidence import save_evidence

    d = journey_db
    _mx(monkeypatch)
    save_evidence(d["vid"], "file", "leads.csv", "name\temail\nCara\tcara@bakery.example", actor_id=d["actor"], size=40)
    preview = asyncio.run(execute_action(d["vid"], "contacts.preview", payload={}, actor_subject=d["actor"]))
    assert "**1 ready to import**" in preview["body_md"] and 'email from "email"' in preview["body_md"]
    stale = asyncio.run(execute_action(d["vid"], "contacts.import", payload={"preview_id": "cimp_old",
        "lawful_basis_attested": True, "lawful_basis": "consent"}, actor_subject=d["actor"]))
    assert "Preview these contacts again" in stale["body_md"] and stale["actions"][0]["action"] == "contacts.preview"


def test_existing_website_review_runs_and_reports_in_chat(journey_db, monkeypatch):
    from app.chat.execute import execute_action
    from app.intake import assets

    d = journey_db
    html = "<h1>Bakery orders</h1><a href='mailto:hi@bakery.example'>Contact</a><p>Book a demo</p>"
    monkeypatch.setattr(assets, "_read_site", lambda url: {"status": "verified", "audit": assets.audit_html(html, fetch_ms=40),
                                                           "evidence_pages": [], "note": "read"})
    offer = assets.chat_asset_reply(d["vid"], "I already have a website: https://bakery.example")
    button = offer["actions"][0]
    assert button["label"] == "Review existing website" and "href" not in button
    reply = asyncio.run(execute_action(d["vid"], button["action"], payload=button["payload"], actor_subject=d["actor"]))
    assert "**Your website**: https://bakery.example" in reply["body_md"]
    assert "Headline: Bakery orders" in reply["body_md"] and "Analytics: not checked" in reply["body_md"]
    assert "1. Check that analytics records visits" in reply["body_md"] and not hidden_hrefs(reply)
    assert assets.get_site(d["vid"])["site"]["url"] == "https://bakery.example"
    monkeypatch.setattr(assets, "_read_site", lambda url: {"status": "unverified", "audit": None, "note": "The page check timed out."})
    failed = asyncio.run(execute_action(d["vid"], "website.review", payload={"url": "https://bakery.example"}, actor_subject=d["actor"]))
    assert "could not read the page" in failed["body_md"] and failed["actions"][0]["action"] == "website.review"


def test_saved_legacy_links_are_served_as_in_chat_actions(journey_db):
    from app.chat.store import _ser
    from app.db.models import ChatMessage

    row = ChatMessage(id="msg_old", venture_id=journey_db["vid"], role="assistant", kind="navigation", body_md="Old reply",
                      actions_json=json.dumps([{"label": "Import contacts", "href": "/gtm?venture=v&import=1"},
                                               {"label": "Open preview", "href": "/v/v/site"}]))
    served = _ser(row)
    assert not hidden_hrefs(served) and [a["action"] for a in served["actions"]] == ["gtm.status", "site.preview"]


def test_specific_hidden_links_keep_their_purpose_in_chat(journey_db, monkeypatch, tmp_path):
    from app.chat.execute import execute_action
    from app.chat.navigation_map import in_chat
    from app.core.local_mode import is_local
    from app.pagebuilder import api
    from tests.unit.chat.test_chat_adversarial import _connect_codex

    converted = in_chat([{"label": "Build my landing page", "href": "/v/ven_1/site?build=1"},
                         {"label": "Team", "href": "/v/ven_1/org"}, {"label": "Open site", "href": "/v/ven_1/site"}])
    assert [a["action"] for a in converted] == ["pagebuilder.build", "agents", "site.preview"]
    quote = {"ready": True, "credits": 1.5, "ceiling_micro": 15000, "quote_fingerprint": "qf_1"}
    monkeypatch.setattr(api, "estimate", lambda venture_id, actor: quote)
    starts = []
    monkeypatch.setattr("app.chat.execute._pagebuilder_start", lambda venture_id, actor, fingerprint, **kwargs:
                        starts.append((venture_id, actor, fingerprint)) or {"kind": "text", "body_md": "Your preview is queued.", "actions": []})
    assert converted[0]["payload"] == {"prepare": True}
    if is_local():
        missing = asyncio.run(execute_action(journey_db["vid"], "pagebuilder.build", payload=converted[0]["payload"], actor_subject=journey_db["actor"]))
        assert any(action.get("href") == "/settings/local#coding-tools" for action in missing["actions"]) and not starts
        _connect_codex(tmp_path, monkeypatch)
    card = asyncio.run(execute_action(journey_db["vid"], "pagebuilder.build", payload=converted[0]["payload"], actor_subject=journey_db["actor"]))
    assert card["kind"] == "text" and "credit" not in card["body_md"].lower()
    expected = [(journey_db["vid"], journey_db["actor"], None if is_local() else "qf_1")]
    assert starts == expected
    reason = "pagebuilder_provider_rejected" if is_local() else "pagebuilder_provider_or_credits_unavailable"
    monkeypatch.setattr(api, "estimate", lambda venture_id, actor: {**quote, "ready": False, "reason": reason})
    blocked = asyncio.run(execute_action(journey_db["vid"], "pagebuilder.build", payload={"prepare": True}, actor_subject=journey_db["actor"]))
    assert blocked["kind"] == "needs_you"
    if is_local():
        assert blocked["actions"] == [{"label": "Retry", "action": "pagebuilder.build", "payload": {"prepare": True}}]
        assert "Settings > Keys and connections > Your AI provider" in blocked["body_md"]
        assert reason not in blocked["body_md"]
    else:
        assert any(action.get("action") == "blocker.explain" for action in blocked["actions"])
    assert starts == expected

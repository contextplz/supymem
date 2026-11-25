"""Landing-page report regressions across business and consumer ideas."""

import asyncio
import json

import pytest
from tests.company_os_harness import local_chat as local_chat, local_publishing as local_publishing

from tests.unit.chat.test_site_actions import env as env


@pytest.fixture(autouse=True)
def _hosted_internal_page_tests(request, monkeypatch):
    if request.node.get_closest_marker("hosted_only"):
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")


@pytest.fixture(params=[
    "Maintenance scheduling for factory equipment teams",
    "Bedtime story suggestions for families with young children",
])
def domain(env, request):
    from app.db.models import Venture
    from app.state import db

    client, venture_id, workspace_id = env
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = request.param
        session.commit()
    return client, venture_id, workspace_id, request.param


async def legacy_page(domain):
    from app.firstlook.service import generate_first_look
    from tests.firstlook_legacy import seed_legacy_page

    _, venture_id, workspace_id, idea = domain
    seed_legacy_page(workspace_id, venture_id)
    return await generate_first_look(workspace_id, idea, venture_id=venture_id, allow_model=False)


def _credit_account(domain):
    from app.core.local_mode import is_local
    from app.credits.accounts import ensure_account
    from app.db.models import CreditAccount, Workspace
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    _, venture_id, workspace_id, _ = domain
    if not is_local():
        return ensure_account(venture_id)
    with db()() as session:
        account = CreditAccount(id=new_id("cra"), scope="venture", venture_id=venture_id,
            workspace_id=workspace_id, owner_person_id=session.get(Workspace, workspace_id).owner_id,
            granted_micro=1_000_000, spent_micro=0, reserved_micro=0, status="active", version=0,
            created_at=utcnow(), updated_at=utcnow())
        session.add(account)
        session.commit()
        session.refresh(account)
        session.expunge(account)
        return account


def test_page_edit_commands_skip_paid_fact_extraction(domain, monkeypatch):
    from types import SimpleNamespace

    from app.ventures import founder_facts as facts
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    if is_local():
        update_settings("capabilities", {"background_work": True}, {})

    _, venture_id, _, idea = domain
    commands = (["Change the headline to Plan every inspection", "Make the subhead more direct", "Update the primary button text to Review equipment",
                 "Change the headline to Manage equipment email reminders", "Rewrite our page copy with a draft inspection schedule"]
                if "factory" in idea else
                ["Set the headline to Find tonight's story", "Rewrite the subheading with A calmer evening", "Change the CTA to Pick a tale",
                 "Update the subheading to Draft a story together", "Improve the website copy for family email invitations"])
    captured, queued = [], []
    monkeypatch.setattr(facts, "_capture_turn", lambda *args: captured.append(args[1]) or ("turn", "workspace", {}, "founder message", None))
    monkeypatch.setattr(facts, "_POOL", SimpleNamespace(submit=lambda *args: queued.append(args)))
    monkeypatch.setattr(facts, "_SLOTS", SimpleNamespace(acquire=lambda **kwargs: True))
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "record")
    monkeypatch.setenv("SANDBOX_MODE", "0")
    for command in commands:
        facts.schedule(venture_id, command, "founder")
    assert captured == commands
    assert queued == []
    statement = "Our operators use paper inspection logs" if "factory" in idea else "Parents choose a story at bedtime"
    facts.schedule(venture_id, statement, "founder")
    assert captured[-1] == statement
    assert len(queued) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["dispatch", "reconciled", "unchanged", "setup", "advice"])
async def test_local_exact_page_edit_starts_from_chat(domain, monkeypatch, outcome):
    import base64

    from types import SimpleNamespace
    from app.chat import router
    from app.chat.intent import classify_intent
    from app.db.models import Job
    from app.pagebuilder import api, tool
    from app.state import db
    from core.clock import utcnow
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _, venture_id, workspace_id, idea = domain
    saved_build(domain, monkeypatch, state="ready")
    wanted = "Try the script library" if "factory" in idea else "Find a story for tonight"
    before = wanted if outcome == "unchanged" else "Welcome"
    with db()() as session:
        row = session.get(Job, "saved-page")
        doc = json.loads(row.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(f"<h1>{before}</h1>".encode()).decode()}
        row.payload_json = json.dumps(doc)
        session.commit()
    instruction = f"Change the headline to {wanted}" if "factory" in idea else f'Change the headline to "{wanted}"'
    assert classify_intent(instruction) == "site.rewrite"
    text = f"Should I {instruction.lower()}?" if outcome == "advice" else instruction
    watch = f"/v/{venture_id}/device-work-orders/dwo_edit"
    calls = []

    def require_connected(actor):
        if outcome == "setup":
            raise HTTPException(409, detail={"code": "pagebuilder_coding_tool_required", "message":
                "Open Settings > Keys and connections > Coding tools, then connect and allow your tool."})

    def rewrite(vid, actor, *, new_version_of, instruction):
        calls.append((vid, actor, new_version_of, instruction))
        if outcome == "reconciled":
            with db()() as session:
                pending = {**doc, "_runtime": {**doc["_runtime"], "state": "running"},
                    "rewrite": {"source_job_id": new_version_of, "instruction": instruction}}
                session.add(Job(id="edited-page", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
                    status="leased", payload_json=json.dumps(pending), created_at=utcnow()))
                session.commit()
            raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain"})
        return {"job_id": "edited-page", "state": "running", "order_id": "dwo_edit", "watch_url": watch}

    async def answer(*args, **kwargs):
        return {"kind": "text", "body_md": "Review your headline before changing it.", "actions": []}

    monkeypatch.setattr(tool, "require_connected", require_connected)
    monkeypatch.setattr(tool, "progress", lambda *_: {"state": "running", "order_id": "dwo_edit", "watch_url": watch})
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: SimpleNamespace(id="founder"))
    monkeypatch.setattr(api, "rewrite", rewrite)
    monkeypatch.setattr("app.core.local_capabilities.hidden_request_message", lambda *_args: None)
    monkeypatch.setattr("app.chat.actions.beta_blocked", lambda *_args: None)
    monkeypatch.setattr(router, "local_capability_reply", lambda _name: None)
    monkeypatch.setattr("app.chat.execute.local_capability_reply", lambda _name: None)
    monkeypatch.setattr("app.chat.assistant.answer_question", answer)
    context = SimpleNamespace(venture={"name": idea, "workspace_id": workspace_id})
    reply = await router._dispatch_intent(venture_id, context, "site.rewrite", text, None, actor_subject="founder")

    assert reply["kind"] != "confirmation"
    assert not any(action.get("action") == "site.rewrite" for action in reply["actions"])
    if outcome in {"dispatch", "reconciled"}:
        assert calls == [(venture_id, "founder", "saved-page", instruction)]
        assert reply["kind"] == "navigation"
        assert reply["action_started"] is True
        assert reply["actions"][0]["href"] == watch
        assert reply["actions"][0]["payload"]["order_id"] == "dwo_edit"
    else:
        assert not calls and not reply.get("action_started")
        expected = {"unchanged": "already has that text", "setup": "Coding tools", "advice": "before changing it"}
        assert expected[outcome] in reply["body_md"]


@pytest.mark.parametrize("markup", ["decoration", "nested", "hidden", "entities", "ambiguous"])
@pytest.mark.hosted_only
def test_literal_headline_edit_retains_decorative_markup(domain, markup):
    from app.pagebuilder.qa import edit_applied, literal_copy_edit

    _, _, _, idea = domain
    previous = "Inspect tools & machines" if "factory" in idea else "Find a gentle bedtime tale"
    wanted = "Keep each workshop ready" if "factory" in idea else "Make room for imagination"
    instruction = f"Change the headline to {wanted}" if "factory" in idea else f"Set your headline to {wanted}"
    encoded = f"&#{ord(previous[0])}{previous[1:]}"
    inner = {"decoration": f'{previous}<span class="bar"></span>',
             "nested": f'<strong>{previous}</strong><span class="bar"></span>',
             "hidden": f'{previous}<span aria-hidden="true">*</span>',
             "entities": f'{encoded}<span class="bar"></span>',
             "ambiguous": '<strong>One phrase</strong> and another phrase'}[markup]
    source = f'<html><body><h1>{inner}</h1><form><input name="email"><button>Join</button></form></body></html>'.encode()
    if markup == "ambiguous":
        with pytest.raises(ValueError, match="page_edit_target_unavailable"):
            literal_copy_edit(source, instruction)
        return
    from html import escape

    expected = source.replace((encoded if markup == "entities" else previous).encode(), escape(wanted, quote=False).encode())
    edited = literal_copy_edit(source, instruction, {"headline": previous})
    assert edited == ("headline", wanted, expected)
    assert edit_applied(source, expected, instruction)


@pytest.mark.parametrize("suffix", ["and keep the design", "without changing the design", ""])
@pytest.mark.hosted_only
def test_literal_replacement_and_verifier_share_preservation_parsing(domain, suffix):
    from app.pagebuilder.qa import edit_applied, literal_copy_edit

    _, _, _, idea = domain
    wanted = "Schedule safer maintenance" if "factory" in idea else "Enjoy a quieter bedtime"
    if not suffix:
        wanted = "Inspect equipment without changing your tools" if "factory" in idea else "Enjoy bedtime without changing your routine"
    separator = " " if suffix.startswith("and") else ", " if suffix else ""
    instruction = (f"Change the headline to {wanted}{separator}{suffix}" if "factory" in idea
                   else f"Update the headline with {wanted}{', ' if suffix else ''}{suffix}")
    before = b'<html><body><h1>Previous heading</h1></body></html>'
    after = before.replace(b"Previous heading", wanted.encode())
    assert literal_copy_edit(before, instruction) == ("headline", wanted, after)
    assert edit_applied(before, after, instruction)


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_routed_copy_edit_is_not_overridden_by_draft_writing(domain, monkeypatch):
    from pathlib import Path

    from app.chat import router
    from app.chat.context import assemble
    from app.chat.idea_bridge import written_request
    from app.firstlook import versions

    _, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    before = b'<html><body><h1>Original heading</h1><p>Original introduction</p></body></html>'
    Path(original.path).write_bytes(before)
    wanted = "Get the inspection list for your workshop" if "factory" in idea else "Explore a reading list for your evening"
    instruction = f"Make the landing page subhead say {wanted}" if "factory" in idea else f"Make our website subhead say {wanted}"
    assert written_request(instruction)
    context = assemble(venture_id)
    reply = await router._dispatch_intent(venture_id, context, "site.rewrite", instruction, "founder", actor_subject="founder")
    saved = versions.get_latest(workspace_id, venture_id=venture_id)
    assert saved.id != original.id, reply
    assert wanted.encode() in Path(saved.path).read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_number", range(5))
@pytest.mark.hosted_only
async def test_ten_plain_edits_deliver_or_report_no_change_without_charging(domain, monkeypatch, edit_number):
    from pathlib import Path

    from app.credits.accounts import get_account_by_id
    from app.firstlook import versions
    from app.state import db

    from tests.unit.chat.test_site_actions import _chat

    client, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    before = Path(original.path).read_bytes()
    requests = (["Change the headline to Keep your equipment dependable",
                 "Set the subhead to Plan inspections in one place and keep the design",
                 "Make the landing page subhead say Get the inspection list for your workshop",
                 f'Change the headline to "{original.copy["headline"]}"',
                 "Rewrite the landing page to explain inspection planning more clearly"] if "factory" in idea else
                ["Update your headline with Discover a new bedtime adventure",
                 "Change the subheading to Find a story for every evening, without changing the design",
                 "Make our website subhead say Explore a reading list for your evening",
                 f'Set the headline to "{original.copy["headline"]}"',
                 "Improve our website copy so choosing a bedtime story feels simpler"])
    account = _credit_account(domain)
    balance = account.granted_micro - account.spent_micro
    paid_calls = []
    if edit_number == 4:
        from app.credits.reserve import reserve, settle
        from app.firstlook import service
        from app.pagebuilder.jobs import EDIT_RECEIPTS

        async def unchanged_paid_copy(*args, **kwargs):
            assert kwargs["instruction"].startswith(requests[edit_number] + " Current headline:")
            hold = reserve(account_id=account.id, amount_micro=137, purpose="firstlook.copy",
                           workspace_id=workspace_id, venture_id=venture_id, task_invocation=original.id)
            settle(hold, actual_micro=137)
            EDIT_RECEIPTS.get().append({"account_id": account.id, "reservation_id": hold.reservation_id,
                                       "task_invocation": original.id, "credits_micro": 137, "settled": True})
            paid_calls.append(hold.reservation_id)
            return {key: value for key, value in original.copy.items() if key != "_render_snapshot"}, 0.0, False

        monkeypatch.setattr(service, "generate_copy", unchanged_paid_copy)
    reply = _chat(client, venture_id, requests[edit_number])
    saved = versions.get_latest(workspace_id, venture_id=venture_id)
    after = Path(saved.path).read_bytes()
    body = reply["body_md"]
    if edit_number == 4:
        assert len(paid_calls) == 1
        assert after == before
    assert "confirmation" not in str([action for action in reply.get("actions", []) if action.get("action") == "site.rewrite"])
    if after == before:
        if edit_number == 3:
            assert "already has that text" in body, reply
        else:
            assert "Nothing changed" in body or "could not" in body or "cannot apply this edit" in body, reply
        with db()() as session:
            final = get_account_by_id(account.id)
            assert final.granted_micro - final.spent_micro == balance
            assert final.reserved_micro == account.reserved_micro
    else:
        from app.pagebuilder.qa import edit_applied, forms_preserved

        assert saved.id != original.id
        assert edit_applied(before, after, requests[edit_number])
        assert forms_preserved(before, after, requests[edit_number])
        assert "not public" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["aria-labelledby", "aria-describedby", "label", "consent", "primary_label"])
@pytest.mark.hosted_only
async def test_edit_preserves_external_form_copy(domain, monkeypatch, reference):
    from pathlib import Path

    from app.chat import site_actions
    from app.firstlook import service, versions
    from app.pagebuilder.qa import forms_preserved

    _, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    consent = ("I agree to receive equipment updates by email." if "factory" in idea
               else "You may email me about new family stories.")
    if reference != "consent":
        consent = "Service notices only" if "factory" in idea else "Stories we pick for you"
    attribute = f'{reference}="terms"' if reference.startswith("aria-") else ""
    outside = (f'<label for="permission">{consent}</label>' if reference == "label"
               else f'<p id="terms">{consent}</p>')
    source = (f'<html><body><h1>{idea}</h1>{outside}<form id="signup" action="/join" method="post">'
              f'<input id="permission" type="checkbox" name="consent" {attribute}>'
              '<button type="submit">Sign up</button></form></body></html>')
    Path(original.path).write_text(source)
    candidate = source.replace(idea, "Explore your next step").replace(consent, "Accept every promotion.")
    if reference == "primary_label":
        source = source.replace('<form id="signup"', '<a href="#signup">Sign up</a><form id="signup"')
        Path(original.path).write_text(source)
        candidate = source.replace(idea, "Explore your next step").replace(">Sign up<", ">Discover your options<")

    async def revised_copy(*args, **kwargs):
        return {**original.copy, "headline": "Explore your next step"}, 0.0, False

    monkeypatch.setattr(service, "generate_copy", revised_copy)
    monkeypatch.setattr(service, "compose_html", lambda **kwargs: (candidate, "test", {}))
    reply = await site_actions._rewrite(venture_id, {"instruction": "Make the headline more direct."}, "founder")
    assert (forms_preserved(source.encode(), candidate.encode()),
            versions.get_latest(workspace_id, venture_id=venture_id).id == original.id,
            reply["kind"]) == (False, True, "needs_you")
    assert "Review" in reply["body_md"]


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_two_edits_do_not_drop_the_unmatched_request(domain, monkeypatch):
    from pathlib import Path

    from app.chat import site_actions
    from app.firstlook import service, versions

    _, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    headline = original.copy["headline"]
    subhead = "Plan service visits together" if "factory" in idea else "Find a tale for your evening"
    source = f'<html><body><h1>{headline}</h1><p>Previous introduction</p><form action="/join"></form></body></html>'
    Path(original.path).write_text(source)
    instruction = (f'Change the headline to "{headline}" and update the subhead to "{subhead}".'
                   if "factory" in idea else f'Set the headline to "{headline}", and change the subheading to "{subhead}".')

    async def revised_copy(*args, **kwargs):
        assert kwargs["instruction"] and instruction in kwargs["instruction"]
        return {**original.copy, "subhead": subhead}, 0.0, False

    monkeypatch.setattr(service, "generate_copy", revised_copy)
    monkeypatch.setattr(service, "compose_html", lambda **kwargs: (source.replace("Previous introduction", subhead), "test", {}))
    reply = await site_actions._rewrite(venture_id, {"instruction": instruction}, "founder")
    saved = versions.get_latest(workspace_id, venture_id=venture_id)
    assert saved.id != original.id and subhead in Path(saved.path).read_text()
    assert "changed" in reply["body_md"].lower() or "Headline:" in reply["body_md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_action,owner_override", [(True, False), (False, False), (False, True)])
@pytest.mark.hosted_only
async def test_shared_primary_label_only_edits_the_same_action(domain, same_action, owner_override):
    from pathlib import Path

    from app.chat import site_actions
    from app.firstlook import versions
    from app.pagebuilder.qa import literal_copy_edit

    _, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    label = "Ask for a maintenance slot" if "factory" in idea else "Request story ideas"
    current = original.copy["cta"]["label"]
    target = "#signup" if same_action or owner_override else "/other-action"
    owner = ' form="other"' if owner_override else ""
    source = (f'<html><body><h1>{idea}</h1><nav><a href="/about">About this idea</a></nav><a href="{target}">{current}</a>'
              f'<form id="signup" action="/join" method="post"><input type="email" required>'
              f'<button type="submit"{owner}>{current}</button></form>'
              '<form id="other" action="/other-action"></form></body></html>')
    Path(original.path).write_text(source)
    instruction = (f'Change your primary button to "{label}".' if "factory" in idea
                   else f'Make the main button say "{label}".')
    reply = await site_actions._rewrite(venture_id, {"instruction": instruction}, "founder")
    saved = versions.get_latest(workspace_id, venture_id=venture_id)
    assert (saved.id != original.id) is same_action
    if same_action:
        assert Path(saved.path).read_text() == source.replace(current, label)
        assert literal_copy_edit(source.encode(), instruction)[2] == source.replace(current, label).encode()
    else:
        assert reply["kind"] == "needs_you" and "Review" in reply["body_md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", ["remove_form", "change_action", "remove_consent", "change_consent"])
@pytest.mark.hosted_only
async def test_page_edit_preserves_form_and_managed_signup_requires_consent(domain, monkeypatch, contract):
    import hashlib
    from html import escape
    from pathlib import Path

    from app.chat import site_actions
    from app.conversion import waitlist
    from app.firstlook import service, versions

    client, venture_id, workspace_id, idea = domain
    original = await legacy_page(domain)
    _, _, name = site_actions._venture(venture_id)
    consent = waitlist.consent_text(name)
    digest = hashlib.sha256(consent.encode()).hexdigest()
    consent_form = (f'<form method="post" action="{waitlist.public_url(venture_id)}">'
        '<input type="email" name="email" required>'
        '<label><input type="checkbox" name="consent" value="yes" required>'
        f'{escape(consent)}</label>'
        f'<input type="hidden" name="consent_version" value="{waitlist.VERSION}">'
        f'<input type="hidden" name="consent_digest" value="{digest}">'
        '<button type="submit">Request updates</button></form>')
    source = f'<html><body><h1>{escape(idea)}</h1>{consent_form}</body></html>'
    Path(original.path).write_text(source)
    replacement = {"remove_form": "", "change_action": consent_form.replace(waitlist.public_url(venture_id), "/interest"),
        "remove_consent": consent_form.replace('<input type="checkbox" name="consent" value="yes" required>', ""),
        "change_consent": consent_form.replace(escape(consent), "Receive all promotional offers.")}[contract]
    candidate = source.replace(escape(idea), "Explore your next step").replace(consent_form, replacement)
    async def revised_copy(*args, **kwargs):
        return {**original.copy, "headline": "Explore your next step"}, 0.0, False
    monkeypatch.setattr(service, "generate_copy", revised_copy)
    monkeypatch.setattr(service, "compose_html", lambda **kwargs: (candidate, "test", {}))
    reply = await site_actions._rewrite(venture_id,
        {"instruction": "Rewrite our landing page to be shorter while retaining the existing form."}, "founder")
    latest = versions.get_latest(workspace_id, venture_id=venture_id)
    signup = client.post(f"/public/waitlists/{venture_id}", json={"email": "synthetic@consent.test"})
    assert (latest.id == original.id, Path(latest.path).read_text() == source,
            reply["kind"], signup.status_code) == (True, True, "needs_you", 422)
    assert "form" in reply["body_md"] and "Review" in reply["body_md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["no_op", "rejected"])
@pytest.mark.hosted_only
async def test_edit_preflight_skips_paid_routing_and_unchanged_versions(domain, monkeypatch, outcome):
    from app.chat import llm_router
    from app.firstlook import versions

    from tests.unit.chat.test_site_actions import _chat

    client, venture_id, workspace_id, _idea = domain
    original = await legacy_page(domain)
    routed = []

    async def paid_route(*args, **kwargs):
        routed.append(args)

    monkeypatch.setattr(llm_router, "route", paid_route)
    text = (f'Update our headline to "{original.copy["headline"]}" and retain all current styling, images, navigation and spacing.'
            if outcome == "no_op" else "Rewrite our landing page using this guidance: " + "retain the current spacing " * 30)
    _chat(client, venture_id, text)
    latest = versions.get_latest(workspace_id, venture_id=venture_id)
    assert (len(routed), latest.id == original.id) == (0, True)


def test_ordinary_primary_button_edits_use_shared_matcher(domain):
    from app.chat import site_actions

    label = "View maintenance windows" if "factory" in domain[3] else "Choose tonight's story"
    requests = [f'Make our primary button say "{label}" while preserving its appearance.',
                f'Update the main button label to "{label}" without changing its appearance.']
    assert [site_actions.classify_site_intent(text) for text in requests] == ["site.rewrite", "site.rewrite"]


def saved_build(domain, monkeypatch, *, state="ready", stage="ready"):
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id, idea = domain
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "1")
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    document = {"variant_count": 1, "_runtime": {"state": state, "stage": stage, "progress": .4,
        "operations": {}, "calls_used": 0, "quote": {"ceiling_micro": 10000, "payer": "platform_credits"}},
        "result": {"bundles": [{"direction": idea}]}}
    with db()() as session:
        session.add(Job(id="saved-page", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
            payload_json=json.dumps(document), status="succeeded" if state == "ready" else "leased",
            created_at=utcnow()))
        session.commit()


def test_local_page_edit_credentials_never_reach_jobs_or_device_orders(domain, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select
    from tests.company_os_harness import company_os_test_client

    from app.api.chat_api import router
    from app.build.device_orders import KIND as ORDER_KIND
    from app.chat import site_actions
    from app.db.models import Job, VentureDocument, Workspace
    from app.pagebuilder import api
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [])
    _, venture_id, workspace_id, idea = domain
    saved_build(domain, monkeypatch)
    instruction = f"Change the headline for {idea} to password=synthetic-mock-value"

    with db()() as session:
        actor = session.get(Workspace, workspace_id).owner_id
    with monkeypatch.context() as admission:
        admission.setattr(api, "require_enabled", lambda: pytest.fail("Reject before build admission"))
        for field in ("instruction", "rewrite_instruction"):
            with pytest.raises(HTTPException) as error:
                api._start(venture_id, actor, new_version_of="saved-page", **{field: instruction})
            assert error.value.status_code == 409
            assert "Settings > Keys and connections" in error.value.detail["message"]
            assert "synthetic-mock-value" not in str(error.value.detail)

    app = FastAPI()
    app.include_router(router)
    response = company_os_test_client(app).post(f"/ventures/{venture_id}/chat/confirm", headers={"X-Subject": actor},
        json={"action": "site.rewrite", "payload": {"instruction": instruction}, "idempotency_key": "reject-page-credential"})
    assert response.status_code == 200, response.json().get("detail")
    assert "Remove credentials" in response.json()["body_md"]
    assert any(action.get("href") == "/settings/local" for action in response.json()["actions"])
    assert "synthetic-mock-value" not in response.text
    reply = site_actions.route(venture_id, "site.rewrite", instruction)
    assert "Settings > Keys and connections" in reply["body_md"] and "synthetic-mock-value" not in str(reply)

    with db()() as session:
        assert list(session.scalars(select(Job.id).where(Job.kind == api.KIND, Job.venture_id == venture_id))) == ["saved-page"]
        assert not session.scalar(select(VentureDocument.id).where(VentureDocument.kind == ORDER_KIND,
            VentureDocument.venture_id == venture_id))


@pytest.mark.parametrize("outcome", ["unchanged", "uncertain", "ready", "rejected_before_reply", "delivered"])
@pytest.mark.hosted_only
def test_queued_edit_routing_refund_is_durable_and_idempotent(domain, monkeypatch, outcome):
    import base64

    from app.chat import llm_router, router, site_actions
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import reserve, settle
    from app.db.models import Job
    from app.pagebuilder import api, jobs, runtime
    from app.state import db
    from core.clock import utcnow

    from tests.unit.chat.test_site_actions import _chat

    client, venture_id, workspace_id, idea = domain
    saved_build(domain, monkeypatch)
    with db()() as session:
        source = session.get(Job, "saved-page")
        doc = json.loads(source.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(f"<h1>{idea}</h1>".encode()).decode()}
        source.payload_json = json.dumps(doc)
        session.commit()
    account = _credit_account(domain)
    balance = account.granted_micro - account.spent_micro
    instruction = ("Could you simplify the introduction for equipment planners?" if "factory" in idea
                   else "Please give the opening copy a gentler voice for parents.")
    receipts = []

    async def routed(*args, **kwargs):
        hold = reserve(account_id=account.id, amount_micro=137, purpose="chat.router",
                       workspace_id=workspace_id, venture_id=venture_id, task_invocation="routing-regression")
        settle(hold, actual_micro=137)
        usage = {"account_id": account.id, "reservation_id": hold.reservation_id,
                 "task_invocation": "routing-regression", "settled": True, "credits_micro": 137}
        receipts.append(usage)
        jobs.EDIT_RECEIPTS.get().append(usage)
        return llm_router.Decision([("site.rewrite", instruction)])

    def queued(vid, actor, *, new_version_of, instruction):
        with db()() as session:
            doc = json.loads(session.get(Job, new_version_of).payload_json)
            doc["rewrite"] = {"source_job_id": new_version_of, "instruction": instruction}
            doc["_runtime"].update(state="failed" if outcome == "rejected_before_reply" else "queued",
                                   error="pagebuilder_edit_unchanged")
            session.add(Job(id="routed-edit", kind=api.KIND, workspace_id=workspace_id, venture_id=vid,
                            payload_json=json.dumps(doc), status="queued", created_at=utcnow()))
            session.commit()
        return {"job_id": "routed-edit"}

    async def dispatch(vid, context, intent, text, speaker, **kwargs):
        assert intent == "site.rewrite"
        return await site_actions._rewrite(vid, {"instruction": text}, "founder")

    monkeypatch.setattr(llm_router, "route", routed)
    monkeypatch.setattr(llm_router, "needs_model", lambda *args: True)
    monkeypatch.setattr(site_actions, "classify_site_intent", lambda text: None)
    monkeypatch.setattr(api, "rewrite", queued)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    reply = _chat(client, venture_id, instruction)
    assert receipts and "_edit_job_id" not in reply
    document = json.loads(runtime.read("routed-edit").payload_json)
    assert document["rewrite"].get("routing_receipts"), "Routing receipt was lost at the queue boundary"
    jobs.record_edit_receipts("routed-edit", receipts, workspace_id, venture_id)
    document["_runtime"]["state"] = "ready" if outcome == "ready" else "failed"
    if outcome == "uncertain":
        document["_runtime"]["operations"] = {"code": {"state": "uncertain"}}
        document["_runtime"]["error"] = "pagebuilder_outcome_uncertain"
    if outcome == "delivered":
        document["result"]["ready_delivered"] = True
    with db()() as session:
        session.get(Job, "routed-edit").payload_json = json.dumps(document)
        session.commit()
    jobs._refund_failed_edit("routed-edit", None)
    jobs._refund_failed_edit("routed-edit", None)
    final = get_account_by_id(account.id)
    assert final.granted_micro - final.spent_micro == balance - (137 if outcome in {"uncertain", "ready", "delivered"} else 0)
    assert final.reserved_micro == account.reserved_micro


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", ["none", "builder", "builder_and_legacy"])
@pytest.mark.hosted_only
async def test_rewrite_checks_current_page_before_offering_paid_legacy_work(domain, monkeypatch, saved):
    from app.chat import site_actions
    from app.firstlook import versions

    _, venture_id, workspace_id, idea = domain
    instruction = f"Rewrite my landing page for {idea}. Keep the claims cautious."
    if saved == "builder_and_legacy":
        await legacy_page(domain)
    if saved != "none":
        saved_build(domain, monkeypatch)
    before = versions.get_latest(workspace_id, venture_id=venture_id)
    reply = site_actions.route(venture_id, "site.rewrite", instruction)
    assert reply["kind"] == "needs_you"
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert any(action.get("action") == ("idea.review" if saved == "none" else "site.preview") for action in reply["actions"])
    assert instruction in reply["body_md"]
    executed = await site_actions.execute(venture_id, "site.rewrite", {"instruction": instruction}, "founder")
    assert executed["body_md"] == reply["body_md"]
    after = versions.get_latest(workspace_id, venture_id=venture_id)
    assert (after.id if after else None) == (before.id if before else None)


@pytest.mark.asyncio
@pytest.mark.hosted_only
async def test_saved_page_edits_read_back_html_before_claiming_changes(domain, monkeypatch):
    import base64

    from app.chat import site_actions
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db

    _, venture_id, workspace_id, _idea = domain
    saved_build(domain, monkeypatch)
    original = '<html><h1>Organize your notes</h1><a href="/consent">Read consent and join</a></html>'
    def encode(text):
        return base64.b64encode(text.encode()).decode()
    with db()() as session:
        row = session.get(Job, "saved-page")
        doc = json.loads(row.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": encode(original)}
        row.payload_json = json.dumps(doc)
        session.commit()
    calls, posted = [], []
    def rewrite(vid, actor, *, new_version_of, instruction):
        from core.clock import utcnow
        calls.append((vid, actor, new_version_of, instruction))
        with db()() as session:
            source = session.get(Job, new_version_of)
            doc = json.loads(source.payload_json)
            doc["rewrite"] = {"source_job_id": new_version_of, "instruction": instruction}
            session.add(Job(id="edited-page", kind=api.KIND, workspace_id=workspace_id,
                            venture_id=vid, status="succeeded", payload_json=json.dumps(doc), created_at=utcnow()))
            session.commit()
        return {"job_id": "edited-page", "state": "ready"}
    monkeypatch.setattr(api, "rewrite", rewrite, raising=False)
    monkeypatch.setattr("app.chat.milestones.post_milestone", lambda *args, **kwargs: posted.append(kwargs))
    for component in ("headline", "subheading", "button text"):
        assert site_actions.classify_site_intent(f'Change the {component} to "Keep your notes together".') == "site.rewrite"
    assert site_actions.classify_site_intent("Change the headline, preserve the layout") == "site.rewrite"
    instruction = 'Change the headline to "Keep your notes together".'
    reply = await site_actions._rewrite(venture_id, {"instruction": instruction}, "founder")
    assert calls == [(venture_id, "founder", "saved-page", instruction)]
    assert "did not change" in reply["body_md"] and "Review" in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        row = session.get(Job, "edited-page")
        doc = json.loads(row.payload_json)
        doc["result"]["bundles"][0]["files"]["index.html"] = encode(original.replace("Organize your notes", "Keep your notes together"))
        row.payload_json = json.dumps(doc)
        session.commit()
    site_actions.page_built("edited-page")
    assert "changed" in posted[-1]["body_md"] and "Review" in posted[-1]["body_md"]
    with db()() as session:
        saved = json.loads(session.get(Job, "edited-page").payload_json)
        html = base64.b64decode(saved["result"]["bundles"][0]["files"]["index.html"]).decode()
    assert "Keep your notes together" in html and '<a href="/consent">Read consent and join</a>' in html


@pytest.mark.asyncio
async def test_edit_ignores_whitespace_and_requires_the_requested_element_to_change(domain, monkeypatch):
    import base64

    from app.chat import site_actions
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id, _ = domain
    saved_build(domain, monkeypatch)
    original = '<main><h1>Organize your notes</h1><p>Your next step</p></main>'
    with db()() as session:
        source = session.get(Job, "saved-page")
        doc = json.loads(source.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(original.encode()).decode()}
        source.payload_json = json.dumps(doc)
        edited = json.loads(json.dumps(doc))
        edited["rewrite"] = {"source_job_id": source.id, "instruction": "Change the headline to Keep your notes together"}
        session.add(Job(id="edited-page", kind=api.KIND, workspace_id=workspace_id,
                        venture_id=venture_id, status="succeeded", payload_json=json.dumps(edited), created_at=utcnow()))
        session.commit()
    for html in (original.replace("<p>", "\n  <p>"), original.replace("Your next step", "Review your next step")):
        with db()() as session:
            row = session.get(Job, "edited-page")
            edited["result"]["bundles"][0]["files"]["index.html"] = base64.b64encode(html.encode()).decode()
            row.payload_json = json.dumps(edited)
            session.commit()
        reply = site_actions._saved_rewrite_reply(venture_id, workspace_id, "edited-page")
        assert reply["kind"] == "needs_you" and "did not change" in reply["body_md"]
        assert "Review" in reply["body_md"]


@pytest.mark.asyncio
async def test_local_copy_uncertain_edit_reconciles_saved_preview_without_another_edit(domain, monkeypatch):
    import base64
    from fastapi import HTTPException
    from app.chat import site_actions
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id, _ = domain
    saved_build(domain, monkeypatch)
    instruction = "Replace the headline with Your next step"
    with db()() as session:
        source = session.get(Job, "saved-page")
        doc = json.loads(source.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(b"<h1>Your old headline</h1>").decode()}
        source.payload_json = json.dumps(doc)
        edited = json.loads(json.dumps(doc))
        edited["rewrite"] = {"source_job_id": source.id, "instruction": instruction}
        edited["_runtime"].update(state="ready", operations={})
        edited["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(b"<h1>Your next step</h1>").decode()}
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(site_actions, "_local_page_setup", lambda actor: None)
    calls = []
    def uncertain(*args, **kwargs):
        calls.append(kwargs)
        with db()() as session:
            if session.get(Job, "local-edit") is None:
                session.add(Job(id="local-edit", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
                                status="succeeded", payload_json=json.dumps(edited), created_at=utcnow()))
                session.commit()
        raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain"})
    monkeypatch.setattr(api, "rewrite", uncertain)
    reply = await site_actions._rewrite(venture_id, {"instruction": instruction}, "founder")
    assert len(calls) == 1
    assert "Your saved landing page changed" in reply["body_md"]
    assert reply["actions"][0]["payload"]["job_id"] == "local-edit"
    with db()() as session:
        edit = session.get(Job, "local-edit")
        doc = json.loads(edit.payload_json)
        doc["_runtime"].update(state="failed", operations={"code": {"state": "uncertain"}})
        edit.payload_json = json.dumps(doc)
        session.commit()
    reply = await site_actions._rewrite(venture_id, {"instruction": instruction}, "founder")
    assert len(calls) == 2
    assert "Choose Review your saved page below" in reply["body_md"] and "before retrying" in reply["body_md"]
    assert any(action.get("action") == "site.preview" for action in reply["actions"])
    assert "contact support" not in reply["body_md"] and "try again with" not in reply["body_md"]
    assert not reply.get("_edit_not_delivered")


@pytest.mark.asyncio
async def test_failed_edit_retries_from_the_latest_ready_source(domain, monkeypatch):
    import base64

    from app.chat import site_actions
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(site_actions, "_local_page_setup", lambda actor: None)
    _, venture_id, workspace_id, _ = domain
    saved_build(domain, monkeypatch)
    with db()() as session:
        source = session.get(Job, "saved-page")
        doc = json.loads(source.payload_json)
        doc["result"]["bundles"][0]["files"] = {"index.html": base64.b64encode(b"<h1>Organize your notes</h1>").decode()}
        source.payload_json = json.dumps(doc)
        failed = json.loads(json.dumps(doc))
        failed["_runtime"].update(state="failed", error="pagebuilder_edit_unchanged")
        failed["rewrite"] = {"source_job_id": source.id, "instruction": "Change the headline"}
        session.add(Job(id="failed-edit", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
                        status="failed_dead", payload_json=json.dumps(failed), created_at=utcnow()))
        session.commit()
    calls = []
    def rewrite(vid, actor, *, new_version_of, instruction):
        calls.append(new_version_of)
        return {"job_id": "failed-edit", "state": "failed"}
    monkeypatch.setattr(api, "rewrite", rewrite)
    reply = await site_actions._rewrite(venture_id, {"instruction": "Change the headline to Keep your notes together"}, "founder")
    assert calls == ["saved-page"]
    assert "could not be applied" in reply["body_md"] and "try again" in reply["body_md"]
    from fastapi import HTTPException
    def unconfirmed(*args, **kwargs):
        calls.append(kwargs["new_version_of"])
        raise HTTPException(409, detail={"code": "pagebuilder_outcome_uncertain"})
    monkeypatch.setattr(api, "rewrite", unconfirmed)
    reply = await site_actions._rewrite(venture_id, {"instruction": "Change the headline"}, "founder")
    assert "Choose Review your saved page below" in reply["body_md"] and "before retrying" in reply["body_md"]
    assert any(action.get("action") == "site.preview" for action in reply["actions"])
    assert "contact support" not in reply["body_md"] and "try again with" not in reply["body_md"]
    assert not reply.get("_edit_not_delivered")
    with db()() as session:
        ready = json.loads(session.get(Job, "saved-page").payload_json)
        ready["rewrite"] = {"source_job_id": "saved-page", "instruction": "Change the headline"}
        session.add(Job(id="ready-edit", kind=api.KIND, workspace_id=workspace_id, venture_id=venture_id,
                        status="done", payload_json=json.dumps(ready), created_at=utcnow()))
        session.commit()
    def previous_edit(*args):
        pytest.fail("An uncertain request must not return an earlier edit as its completed output")
    monkeypatch.setattr(site_actions, "_saved_rewrite_reply", previous_edit)
    reply = await site_actions._rewrite(venture_id, {"instruction": "Change the headline"}, "founder")
    assert calls[-1] == "ready-edit"
    assert "Choose Review your saved page below" in reply["body_md"] and "before retrying" in reply["body_md"]
    assert any(action.get("action") == "site.preview" for action in reply["actions"])
    assert not reply.get("_edit_not_delivered")


@pytest.mark.parametrize("publication_state", [None, "checking", "publishing", "live", "failed"])
@pytest.mark.hosted_only
def test_publish_and_waitlist_followup_review_the_same_saved_builder_candidate(domain, monkeypatch, publication_state):
    from app.api import pagebuilder_api
    from app.chat import site_actions
    from app.core.local_mode import is_local
    from app.tenancy import bind

    _, venture_id, workspace_id, idea = domain
    saved_build(domain, monkeypatch)
    looked_up = []

    def profile(selected_venture, caller):
        looked_up.append((selected_venture, caller))
        return {"current": {"job_id": "saved-page", "state": "ready", "server_publish_ready": True,
            "server_candidates": [{"variant": 0, "direction": idea}],
            "publication": {"state": publication_state} if publication_state else None}}

    requested = []
    monkeypatch.setattr(pagebuilder_api, "profile", profile)
    monkeypatch.setattr("app.pagebuilder_admission.publication.request",
                        lambda _venture, _actor, selection: requested.append(selection) or {"state": "checking"})
    monkeypatch.setattr("app.pagebuilder.local_publish.publish_site", lambda vid, actor, *, job_id=None:
        requested.append({"job_id": job_id}) or {"url": "https://published.example.test", "notice": "Your saved page is published."})
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor="founder"):
        reply = site_actions.route(venture_id, "site.publish", "Publish my landing page")
        next_step, actions = site_actions.after_waitlist(venture_id)
        executed = asyncio.run(site_actions.execute(venture_id, "site.publish", {}, "founder"))
    assert looked_up == [(venture_id, "founder")] * 2
    # Founder decision: asking to publish runs it, on the same saved candidate, with one action to start it.
    assert [(card.get("confirmation") or card)["action"] for card in reply["actions"]] == ["site.publish"]
    if is_local():
        assert requested == [{"job_id": "saved-page"}]
        assert "live at https://published.example.test" in executed["body_md"]
    elif publication_state == "live":
        assert "already live" in executed["body_md"] and not requested
    else:
        assert [(item["job_id"], item["variant"]) for item in requested] == [("saved-page", 0)]
        assert "not public yet" in executed["body_md"]
    assert "No build" not in executed["body_md"]
    assert "build your landing page" not in next_step
    assert actions[0]["action"] == "site.preview" and actions[0]["payload"] == {"job_id": "saved-page"}
    assert not any(action.get("confirmation") for action in actions)
    if publication_state:
        assert {"checking": "checks", "publishing": "not live yet", "live": "live", "failed": "stopped"}[publication_state] in next_step
    else:
        assert "confirm" in next_step and "private" in next_step


@pytest.mark.parametrize("enabled", [True, False])
def test_landing_build_button_uses_the_configured_landing_builder(domain, monkeypatch, enabled):
    from app.chat import site_actions
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "1" if enabled else "0")
    if is_local():
        update_settings("capabilities", {"landing_pages": enabled}, {})
    button = site_actions.build_landing_button()
    available = is_local() or enabled
    assert button["action"] == ("pagebuilder.build" if available else "build.prepare")
    assert button["payload"] == ({"prepare": True} if available else {"hint": "landing page"})
    next_step, actions = site_actions.after_waitlist(domain[1])
    assert "build your landing page" in next_step
    assert actions == [button]


@pytest.mark.asyncio
async def test_reopening_verified_waitlist_reuses_the_valid_receipt(domain, monkeypatch):
    from app.chat import site_actions
    from app.conversion import waitlist
    from app.core.local_mode import is_local

    client, venture_id, workspace_id, _ = domain

    class PublicRoute:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            return client.post(f"/public/waitlists/{venture_id}", **kwargs)

    monkeypatch.setattr(waitlist.httpx, "Client", PublicRoute)
    first = await site_actions.execute(venture_id, "waitlist.enable", {}, "founder")
    if is_local():
        assert "delivery is not verified" in first["body_md"]
        assert "check your inbox" in first["body_md"]
        before = waitlist.status(venture_id, workspace_id)
        assert before["enabled"] and not before["submission_verified"] and before["total"] == 0
        again = await site_actions.execute(venture_id, "waitlist.enable", {}, "founder")
        assert "delivery is not verified" in again["body_md"]
        after = waitlist.status(venture_id, workspace_id)
        assert after["enabled"] and not after["submission_verified"] and after["total"] == 0
        return
    assert "Waitlist on and tested" in first["body_md"]
    before = waitlist.status(venture_id, workspace_id)
    assert before["submission_verified"] and before["total"] == 0
    again = await site_actions.execute(venture_id, "waitlist.enable", {}, "founder")
    assert again["kind"] == "text" and "already" in again["body_md"].lower()
    assert "did not pass" not in again["body_md"] and "Nothing counted as verified" not in again["body_md"]
    after = waitlist.status(venture_id, workspace_id)
    assert after["submission_verified"] and after["verified_at"] == before["verified_at"] and after["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("instruction_length", [500, 600, 1548, 4000])
@pytest.mark.hosted_only
async def test_full_rewrite_instruction_survives_card_and_generation(domain, monkeypatch, instruction_length):
    from app.chat import site_actions
    from app.firstlook import copy

    await legacy_page(domain)
    _, venture_id, _, idea = domain
    start = f"Rewrite my landing page for {idea}. "
    final = "The final button must say Remind me later."
    instruction = start + "x" * (instruction_length - len(start) - len(final)) + final
    assert len(instruction) == instruction_length
    seen = []
    generate = copy.generate_copy

    async def capture(idea, **kwargs):
        seen.append(kwargs.get("instruction"))
        return await generate(idea, **{**kwargs, "allow_model": False})

    monkeypatch.setattr("app.firstlook.service.generate_copy", capture)
    reply = site_actions.route(venture_id, "site.rewrite", instruction)
    if instruction_length > 600:
        assert reply["kind"] == "needs_you" and "600 characters" in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"])
        rejected = await site_actions.execute(venture_id, "site.rewrite", {"instruction": instruction}, "founder")
        assert rejected["body_md"] == reply["body_md"] and not seen
        with pytest.raises(ValueError, match="site_rewrite_instruction_too_long"):
            site_actions.rewrite_card(instruction)
        return
    card = reply["actions"][0]
    assert "confirmation" not in card
    payload = card["payload"]
    assert payload["instruction"] == instruction
    assert not seen
    done = await site_actions.execute(venture_id, "site.rewrite", payload, "founder")
    assert seen and instruction in seen[0]
    assert "not public" in done["body_md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,phrase", [
    ("brief_direction_plan", "designing your page"),
    ("capability_review_1", "checking the claims"),
    ("plan_repair_5", "refining the design"),
    ("code", "creating your page"),
    ("repair_2", "checking your page"),
    ("image_0", "creating the artwork"),
    ("unexpected_internal_stage_42", "working on your page"),
])
async def test_build_progress_uses_plain_phrases_not_internal_stages(domain, monkeypatch, stage, phrase):
    from app.chat import site_actions

    saved_build(domain, monkeypatch, state="running", stage=stage)
    reply = await site_actions.execute(domain[1], "site.preview", {}, "founder")
    assert phrase in reply["body_md"].lower()
    assert stage not in reply["body_md"] and stage.replace("_", " ") not in reply["body_md"]
    assert "40%" in reply["body_md"] and "private" in reply["body_md"]
    assert reply["actions"][0]["action"] == "site.preview"

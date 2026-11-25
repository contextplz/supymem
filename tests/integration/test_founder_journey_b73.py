"""Signed founder journeys with real PostgreSQL, workers, metering and chat routing.

Only the model dial and HTTP search/read transports return synthetic evidence.
shortcut: check supported Markdown payloads, upgrade to browser-free console
renderer assertions when that harness exists; intentional Markdown is allowed.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    pg_database as pg_database,
    service_modules,
)
from tests.security.test_core_signed_boundary import SECRET, headers


FLAGS = ("exploration_v1", "experiments_autopilot_v1", "experiments_quality_v1")
DOMAINS = (
    pytest.param({
        "offer": "A maintenance scheduler for independent dental clinics",
        "customer": "Independent dental clinic owners in Austin, Texas",
        "problem": "Equipment service dates get lost in paper logs",
        "pricing_hypothesis": "A monthly subscription of 35 dollars",
        "desired_outcome": "One clinic agrees to a paid pilot",
        "workflow": "A clinic records equipment and receives service reminders",
        "business": "Cedar Dental Clinic", "market_role": "buyer",
        "website": "https://cedar-clinic.example",
        "category": "dental equipment maintenance",
        "affiliation": "I work at [Cedar Dental Clinic](https://cedar-clinic.example)",
        "need_quote": "We lose equipment service dates in paper logs and need reliable reminders.",
        "public_text": "Cedar Dental Clinic provides dental care in Austin, Texas. I work at [Cedar Dental Clinic](https://cedar-clinic.example). We lose equipment service dates in paper logs and need reliable reminders. You can contact our team at team@cedar-clinic.example.\n800 Clinic Plaza, Austin, TX 78701, United States",
        "alternatives": ("Single-location dental clinic owners in Austin, Texas",
                         "Two-location dental clinic owners in Austin, Texas"),
    }, id="b2b"),
    pytest.param({
        "offer": "An accessible weekend outing planner for families",
        "customer": "Parents planning wheelchair-accessible family outings in Austin, Texas",
        "problem": "Families cannot reliably check accessible facilities before a trip",
        "pricing_hypothesis": "A monthly subscription of 5 dollars",
        "desired_outcome": "One family agrees to a paid trial",
        "workflow": "A parent chooses a destination and checks access information",
        "business": "Accessible Family Outings Group", "market_role": "consumer/individual",
        "website": "https://accessible-outings.example",
        "category": "accessible family outings",
        "affiliation": "",
        "need_quote": "Accessible Family Outings Group is a community for parents in Austin, Texas.",
        "public_text": "Accessible Family Outings Group is a community for parents in Austin, Texas. I plan wheelchair-accessible family outings. Families cannot reliably check accessible facilities before a trip. Ask the moderator before posting an invitation.",
        "alternatives": ("Parents planning wheelchair-accessible park trips in Austin, Texas",
                         "Parents planning wheelchair-accessible museum trips in Austin, Texas"),
    }, id="consumer"),
)


def _completion(data):
    from openai.types import CompletionUsage

    return SimpleNamespace(
        model="DeepSeek-V4-Pro", service_tier="default",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            content=json.dumps(data) if not isinstance(data, str) else data))],
        usage=CompletionUsage(prompt_tokens=300, completion_tokens=150, total_tokens=450),
    )


class Transports:
    def __init__(self, domain):
        self.domain = domain
        self.calls = []
        self.model_requests = []
        self.unhandled = []

    async def model(self, messages, **kwargs):
        purpose = kwargs["purpose"]
        self.calls.append(purpose)
        system = messages[0]["content"]
        try:
            context = json.loads(messages[-1]["content"])
        except (ValueError, TypeError):
            context = {}
        if purpose.startswith("gtm."):
            self.model_requests.append({"purpose": purpose, "context": context})
        fields = {key: self.domain[key] for key in (
            "offer", "customer", "problem", "workflow", "pricing_hypothesis", "desired_outcome")}
        if purpose == "intake.classify":
            data = {"path": "startup", "confidence": .95,
                    "extracted": {"idea": fields["offer"], "audience": fields["customer"], "stage": "idea"},
                    "founder_line": "You can clarify this idea before choosing any paid work.",
                    "button_label": "Clarify my idea"}
        elif purpose == "idea.clarify_turn":
            understanding = {**fields, **context.get("understanding", {})}
            answered = context.get("answered_fields", {})
            field = "desired_outcome" if answered.get("pricing_hypothesis") else "pricing_hypothesis"
            understanding.pop(field, None)
            data = {"case": "know", "action": "proposal" if context.get("force_propose") else "question",
                    "message": "You can review your saved idea and assumptions.",
                    "understanding": understanding,
                    "question": None if context.get("force_propose") else {
                        "text": "What result do you want first?" if field == "desired_outcome" else "How would you price this?",
                        "field": field, "why": "Choose a testable starting point", "options": []}}
        elif "short GStack office-hours session" in system:
            data = {"questions": [
                {"id": "Q1", "question": "Has anyone agreed to pay for your proposed solution?", "options": ["Not yet", "One person"]},
                {"id": "Q2", "question": "How will you reach your first trial participant?", "options": ["A local group", "An introduction"]}]}
        elif purpose == "idea.validate":
            from tests.idea_fixtures import validation_completion

            return validation_completion(messages)
        elif purpose == "memory.extract":
            from app.ventures.founder_facts import Extraction

            data = dict.fromkeys(Extraction.model_fields)
            turn = context.get("turn", "")
            source = re.search(r"Source must be ([^.]+)\.", system)[1]
            for slot, field in (("audience", "customer"), ("problem", "problem"), ("pricing", "pricing_hypothesis")):
                value = fields[field]
                if value in turn:
                    data[slot] = {"value": value, "source": source, "quote": value, "confidence": .95}
        elif purpose == "research.prior_art.plan":
            data = {"category_terms": [self.domain["category"]], "job_phrases": [self.domain["category"]],
                    "customer_terms": [], "seed_competitors": [],
                    "negative_terms": [], "consumer_mobile": False}
        elif purpose == "research.deep":
            sources = context.get("sources", [])
            data = {"overview": "You have public sources to review, not proof of demand.",
                    "observations": [{"source_id": source["id"], "quote_index": 0} for source in sources[:2]],
                    "assessments": [], "competitor_checks": [], "customer_reviews": [],
                    "service_checks": [], "coverage_gaps": ["No verified payment evidence."],
                    "proposed_tests": ["Ask one eligible participant to try a paid pilot."]}
        elif purpose == "experiment.exploration":
            belief_type = re.search(r"Return beliefs of type (\w+)", system)[1]
            audiences = (fields["customer"], *self.domain["alternatives"])
            data = {"beliefs": [{"type": belief_type,
                "statement": f"You could test demand from {audience.lower()}.",
                "confidence": .2, "audience": audience, "problem": fields["problem"]} for audience in audiences]}
        elif purpose == "experiment.beliefs":
            from app.experiments.beliefs import TYPES

            data = {"beliefs": [{"type": belief_type,
                "statement": f"You can test {belief_type.replace('_', ' ')} with {fields['customer'].lower()} using trial {index + 1}.",
                "confidence": .2, "audience": fields["customer"], "problem": fields["problem"]}
                for index, belief_type in enumerate((*TYPES, TYPES[0]))]}
        elif purpose == "venture.chat.assistant" and "saved_audience_ok" in system:
            data = {"saved_audience_ok": True, "groups": [{"who": audience, "problem": fields["problem"],
                "why": "You can interview this group", "where": "Local community groups", "reach": "Ask the group moderator"}
                for audience in (fields["customer"], *self.domain["alternatives"])]}
        elif purpose == "venture.chat.assistant" and system.startswith("You name products."):
            data = self.domain["category"].split()[0].title() + " Trial"
        elif purpose.startswith("gtm.discovery."):
            candidates = context.get("candidates", [])
            if '"role_in_market"' in system:
                data = {"role_in_market": self.domain["market_role"], "icp_fit": .95,
                        "anti_icp": False, "homepage_quote": self.domain["public_text"].split(". ")[0] + "."}
            elif '"relevance"' in system:
                data = {"items": [{"id": item["id"], "relevance": .95
                    if self.domain["need_quote"] in item.get("text", "") else .1} for item in candidates]}
            elif "queries" in system and purpose.rsplit(".", 1)[-1] in {"plan", "reformulate"}:
                data = {"queries": [{"query": fields["customer"] + " " + suffix, "intent": intent}
                    for suffix, intent in (("discussion", "customer_voice"), ("community", "community"))]}
            elif purpose.endswith(("draft", "reachdraft")):
                data = {"items": [{"id": item.get("id", index),
                    "body": "Could you describe how you handle this today? You can say no. I am testing an idea, not claiming it works.",
                    **({"connection_note": "Could I ask about your current approach?"} if purpose.endswith("reachdraft") else {})}
                    for index, item in enumerate(candidates[:8])]}
            else:
                data = {"items": [{"id": item["id"],
                    "kind": "customer_voice" if self.domain["market_role"] == "buyer" else "channel",
                    "quote": self.domain["need_quote"], "affiliation": self.domain["affiliation"],
                    "name": self.domain["business"],
                    "url": re.sub(r"^<untrusted>\s*|\s*</untrusted>$", "", item.get("url", "")),
                    "role": "", "company": "", "location": "Austin, Texas", "fit": 95,
                    "why": "You could learn how this group currently handles the problem.",
                    "value_exchange": "You could share your research findings after a short conversation."}
                    for item in candidates]}
        elif purpose == "venture.chat.route":
            if "Original message:\n" in messages[-1]["content"]:
                data = messages[-1]["content"].split("Original message:\n", 1)[1]
            else:
                data = {"acts": [{"intent": "assistant", "text": context.get("founder_message", "")}], "clarify": ""}
        elif "name" in purpose:
            data = {"name": "Field Trial", "reason": "A temporary name for your trial"}
        elif purpose == "venture.chat.assistant":
            data = "You can review your saved work here. Nothing has been sent."
        else:
            self.unhandled.append({"purpose": purpose, "system": system[:600], "context": context})
            raise AssertionError(f"Unimplemented model transport fixture: {purpose}")
        return _completion(data)

    def http(self, request):
        from core.clock import utcnow

        published = (utcnow() - timedelta(days=1)).isoformat()
        host = request.url.host
        self.calls.append(str(request.url))
        if host == "search.example.test":
            body = json.loads(request.content)
            text = self.domain["public_text"]
            if request.url.path.endswith("scrape"):
                data = {"success": True, "data": {"markdown": text,
                    "rawHtml": "<html><body>" + text + "</body></html>",
                    "metadata": {"title": self.domain["business"], "sourceURL": body["url"], "publishedTime": published}}}
            else:
                data = {"success": True, "data": {"web": [{
                    "url": f"{self.domain['website']}/{index}", "title": self.domain["business"],
                    "description": text, "markdown": text,
                    "metadata": {"publishedTime": published}} for index in range(3)]}}
        elif host == "hn.algolia.com":
            data = {"hits": [], "nbHits": 0}
        elif host == "api.github.com":
            data = {"items": [], "total_count": 0, "incomplete_results": False}
        elif host == "yc-oss.github.io":
            data = []
        elif host in {"overpass-api.de", "overpass.kumi.systems"}:
            data = {"elements": []}
        else:
            self.unhandled.append({"url": str(request.url)})
            raise AssertionError(f"Unimplemented search transport fixture: {request.url}")
        return httpx.Response(200, json=data, request=request)


@pytest.fixture
def founder(pg_database, tmp_path, monkeypatch, request, caplog):
    domain, flag = request.param
    url, engine = pg_database
    settings = {"APP_ENV": "local", "COMPANY_OS_DB_URL": url, "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "working.db"),
        "COS_INTERNAL_ASSERTION_SECRET": SECRET, "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1",
        "ADMIN_REQUIRE_TOTP": "1", "ADMIN_BOOTSTRAP_EMAILS": "journey-operator@example.test",
        "COMPANY_OS_VAULT_SECRET": "x" * 64,
        "PROVISIONING_MODE": "local", "WILL_SCHEDULER": "0", "IDEA_PAGE_REFRESH": "off",
        "CONTENT_ENGINE_ENABLED": "0", "CONTENT_AGENT_ENABLED": "0", "CHANNELS_DRY_RUN": "true",
        "LLM_CREDITS_REQUIRE_GATE": "1",
        "FIRECRAWL_SELF_HOSTED_URL": "https://search.example.test", "FIRECRAWL_SELF_HOSTED_TOKEN": "fixture-token"}
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app import state
        from app.authz.workspace import ensure_subject_person
        from app.credits.policy import get_policy, set_policy
        from app.main import create_app
        from app.memory.pg_working import PostgresWorkingMemory
        from llm_gateway import sole
        from tests.idea_fixtures import install_provider

        factory = sessionmaker(engine, expire_on_commit=False)
        state.set_factory(factory)
        monkeypatch.setattr(state, "MEM", PostgresWorkingMemory())
        actor = "usr_journey_" + uuid.uuid4().hex
        ensure_subject_person(actor)
        previous = get_policy("feature_flags") or {}
        set_policy("feature_flags", {**previous, **{name: name == flag for name in FLAGS}}, actor)
        transports = Transports(domain)
        install_provider(monkeypatch)
        monkeypatch.setattr(sole, "_primary_chat", transports.model)
        original_send = httpx.Client.send

        async def async_send(client, request, **kwargs):
            return transports.http(request)

        def sync_send(client, request, **kwargs):
            if isinstance(client, TestClient):
                return original_send(client, request, **kwargs)
            return transports.http(request)

        monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
        monkeypatch.setattr(httpx.Client, "send", sync_send)
        try:
            with TestClient(create_app(), raise_server_exceptions=False) as client:
                journey = Journey(client, factory, actor, domain, flag, transports, tmp_path)
                journey.caplog = caplog
                yield journey
        finally:
            set_policy("feature_flags", previous, actor)


class Journey:
    def __init__(self, client, factory, actor, domain, flag, transports, tmp_path):
        self.client, self.factory, self.actor = client, factory, actor
        self.domain, self.flag, self.transports = domain, flag, transports
        self.vid = None
        self.answers = []
        self.answered_fields = {"offer", "customer", "problem"}
        self.failures = []
        self.assertions = 0
        self.steps = []
        self.artifact = tmp_path / "journey.json"

    def check(self, condition, step, rule, detail):
        self.assertions += 1
        if not condition:
            self.failures.append({"step": step, "rule": rule, "detail": detail})

    def request(self, step, path, body=None, method="POST"):
        response = self.client.request(method, path, json=body, headers=headers(path, method, self.actor))
        self.check(response.status_code == 200, step, "working next step", f"{path}: {response.status_code} {response.text}")
        result = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        self.steps.append({"step": step, "reply": result})
        if path == "/entry/chat/preview":
            from app.db.supymem_models import EntryChatProposal

            proposal = result.get("proposal", result)
            text = "\n".join(str(proposal.get(key) or "") for key in ("message", "onboarding_question", "button_label"))
            self.copy_checks(step, text, [])
            self.check(proposal.get("onboarding_field") not in self.answered_fields,
                step, "no question already answered", str(proposal))
            self.check(bool(proposal.get("button_label") or proposal.get("onboarding_question")),
                step, "working next step", str(proposal))
            with self.factory() as session:
                saved = session.get(EntryChatProposal, proposal.get("id")) if proposal.get("id") else None
                self.check(bool(saved and saved.state == proposal.get("state", "pending")),
                    step, "reply agrees with saved state", str(proposal))
        elif path == "/entry/chat/confirm" and result.get("venture_id"):
            from app.db.models import Venture
            from app.db.supymem_models import EntryChatProposal

            with self.factory() as session:
                proposal = session.get(EntryChatProposal, body["proposal_id"])
                venture = session.get(Venture, result["venture_id"])
                self.check(proposal.state == "committed" and venture.workspace_id == result["workspace_id"],
                    step, "reply agrees with saved state", str(result))
        return result

    def chat(self, step, text, **extra):
        self.answers.append(text)
        reply = self.request(step, f"/ventures/{self.vid}/chat", {"message": text, **extra})
        self.audit(step, reply)
        return reply

    def tap(self, step, reply, action):
        if action.get("action") == "chat.reply":
            payload = action.get("payload") or {}
            return self.chat(step, action.get("text") or payload.get("text") or action["label"],
                **({"question_id": payload["question_id"]} if payload.get("question_id") else {}))
        confirmation = action.get("confirmation")
        if (confirmation or {}).get("action") == "gtm.route.confirm":
            result = self.request(step, f"/ventures/{self.vid}/chat/gtm/route/confirm",
                {"proposal_id": confirmation["resulting_state"]["proposal_id"]})
            self.audit(step, result)
            return result
        body = {"action": confirmation["action"] if confirmation else action["action"],
                "payload": confirmation.get("resulting_state", {}) if confirmation else action.get("payload", {})}
        if confirmation:
            body["message_id"] = reply["id"]
        result = self.request(step, f"/ventures/{self.vid}/chat/confirm", body)
        self.audit(step, result)
        return result

    def action(self, reply, names):
        return next((item for item in reply.get("actions", [])
            if (item.get("confirmation") or {}).get("action", item.get("action")) in names), None)

    def work(self, step, kinds):
        from app.core.jobs import lease
        from app.db.models import Job
        from app.worker.handlers import install_default_handlers
        from app.worker.main import _run_one

        install_default_handlers()
        for attempt in range(20):
            with self.factory() as session:
                jobs = lease(session, "founder-journey-worker", kinds=kinds, n=1)
                session.commit()
            if not jobs:
                break
            asyncio.run(_run_one(self.factory, jobs[0], {jobs[0].id}))
            with self.factory() as session:
                saved = session.get(Job, jobs[0].id)
                self.check(saved.status == "succeeded", step, "working next step", f"Worker {saved.kind}: {saved.status} {saved.last_error}")

    def copy_checks(self, step, body, actions):
        body = re.sub(r"^\s*<!--employee:[^>]*-->\s*", "", body)
        parts = [body]
        for action in actions:
            confirmation = action.get("confirmation") or {}
            parts.extend([action.get("label", ""), confirmation.get("interpretation", ""),
                confirmation.get("cancel_label", ""), *confirmation.get("consequences", [])])
        visible = " ".join(parts)
        self.check(not re.search(r"\b\d[\d,.]*\s+credits?\b", visible, re.I),
            step, "no visible usage prices", visible)
        self.check(bool(body.strip()), step, "answers visible", "Empty founder reply")
        self.check(not re.search(r"<\/?[a-zA-Z][^>]*>|```(?:json|html|xml)|\\n|\{\s*\"(?:action|understanding|intent)\"", visible),
            step, "no raw markup", visible)
        internal = re.search(r"\b(?:gstack|dossier|workspace|ICP|supymem|idempotency|feature.flag|revision_id|experiment_id|queued|worker)\b|\b[a-z]+(?:_[a-z]+)+\b", visible, re.I)
        self.check(not internal, step, "no internal words", f"{internal[0] if internal else ''}: {visible}")
        self.check("\u2014" not in visible, step, "no em dash", visible)
        return body

    def audit(self, step, reply):
        from app.chat.actions import disabled_reason
        from app.core.jobs import ACTIVE_JOB_STATUSES
        from app.db.models import ChatMessage, GtmTouch, Job
        from app.db.supymem_models import ClarifierTurn, Experiment, IdeaRevision, IdeaSession, ResearchDossier

        actions = reply.get("actions", [])
        if not any(item.get("question") for item in reply.get("citations", []) if isinstance(item, dict)):
            actions = [*actions, *(reply.get("next_moves") or {}).get("actions", [])]
        body = self.copy_checks(step, reply.get("body_md", ""), actions)
        with self.factory() as session:
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == self.vid)
                .order_by(IdeaSession.created_at.desc()).limit(1))
            revision = session.get(IdeaRevision, idea.current_revision_id) if idea else None
            experiments = list(session.scalars(select(Experiment).where(Experiment.venture_id == self.vid)))
            dossiers = list(session.scalars(select(ResearchDossier).where(ResearchDossier.venture_id == self.vid)))
            self.steps[-1]["saved_state"] = {"idea": idea.state if idea else None,
                "answers": json.loads(revision.structured) if revision else {},
                "research": [{"id": row.id, "state": row.state} for row in dossiers],
                "experiments": [{"id": row.id, "state": row.state, "hypothesis": row.hypothesis} for row in experiments]}
            history = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == self.vid)
                .order_by(ChatMessage.created_at)))
            founder_text = "\n".join(message.body_md for message in history if message.role in {"user", "founder"})
            for answer in self.answers:
                self.check(answer in founder_text, step, "answers visible", f"Missing answer in saved chat: {answer}")
            self.check(not session.scalar(select(GtmTouch.id).where(GtmTouch.venture_id == self.vid, GtmTouch.direction == "out")),
                step, "confirmation before sending", "An outbound touch exists before send confirmation")
            active = session.scalar(select(Job.id).where(Job.venture_id == self.vid,
                Job.status.in_(ACTIVE_JOB_STATUSES)))
            for action in actions:
                name = (action.get("confirmation") or {}).get("action", action.get("action"))
                if step == "customer search" and name == "gtm.find":
                    self.check(not action.get("confirmation"), step,
                        "requested search needs no confirmation", str(action))
                if name and name != "chat.reply":
                    self.check(disabled_reason(name) is None, step, "working next step", f"Unavailable action: {name}")
                if idea and idea.state == "confirmed":
                    self.check(name not in {"idea.review", "idea.confirm", "idea.proposal.approve"},
                        step, "no button that sends you back", str(action))
                question_id = (action.get("payload") or {}).get("question_id")
                question = session.get(ClarifierTurn, question_id) if question_id else None
                field = json.loads(question.options or "{}").get("field") if question else None
                self.check(field not in self.answered_fields, step, "no question already answered", str(action))
            if idea:
                self.check(not (idea.state == "confirmed" and re.search(r"(?:before|first|need to).*confirm your idea", body, re.I)),
                    step, "reply agrees with saved state", f"Saved idea is confirmed: {body}")
                latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                    .order_by(ClarifierTurn.turn.desc()).limit(1))
                field = json.loads(latest.options or "{}").get("field") if latest else None
                self.check(not (latest and latest.role == "agent" and latest.kind == "question"
                    and latest.text in body and field in self.answered_fields),
                    step, "no question already answered", body)
            self.check(not re.search(r"\b(?:I|we|you)(?:'ve| have)? (?:sent|emailed|published|launched)\b", body, re.I),
                step, "reply agrees with saved state", "No sends or publication were confirmed: " + body)
            self.check(not any(row.state == "running" for row in experiments), step,
                "no unrequested experiment execution", "Designing an experiment must not run it")
            search_job = session.scalar(select(Job.id).where(Job.venture_id == self.vid,
                Job.kind == "gtm.journey.prepare"))
            self.check(not search_job or step in {"customer search", "drafts", "asking to send"}, step,
                "no unrequested search", "A suggested search started before you asked for it")
        progressing = active and (reply.get("kind") in {"idea_clarification_pending", "gtm_search_progress"}
                                  or reply.get("action_started") is True)
        self.check(bool(actions) or bool(progressing) or bool(re.search(r"\?|\b(?:reply|say|tell me|try again|retry|connect|choose)\b", body, re.I)),
            step, "working next step", body)

    def finish(self):
        from app.db.models import VentureDocument
        from app.db.supymem_models import ResearchDossier

        with self.factory() as session:
            documents = [{"kind": row.kind, "body": row.body_md, "provenance": row.provenance_json}
                for row in session.scalars(select(VentureDocument).where(VentureDocument.venture_id == self.vid))]
            research = [{"state": row.state, "summary": row.summary}
                for row in session.scalars(select(ResearchDossier).where(ResearchDossier.venture_id == self.vid))]
        result = {"flag": self.flag, "domain": self.domain, "steps": self.steps,
                  "assertions": self.assertions,
                  "failures": self.failures, "transport_calls": self.transports.calls,
                  "model_requests": self.transports.model_requests,
                  "documents": documents, "research": research,
                  "logs": [{"message": row.getMessage(), "exception_type": getattr(row, "exception_type", None)}
                    for row in self.caplog.records if row.levelno >= 30],
                  "fixture_errors": self.transports.unhandled}
        self.artifact.write_text(json.dumps(result, indent=2, default=str))
        assert not self.transports.unhandled, f"Transport fixture incomplete: {self.artifact}: {self.transports.unhandled}"
        assert not self.failures, f"Product bugs ({len(self.failures)}); transcript: {self.artifact}\n" + "\n".join(
            f"{failure['step']}: {failure['rule']}: {failure['detail']}" for failure in self.failures)


@pytest.mark.parametrize("founder", [pytest.param((domain.values[0], flag),
    id=f"{domain.id}-{flag}") for domain in DOMAINS for flag in FLAGS], indirect=True)
def test_founder_journey_through_real_chat(founder):
    journey = founder
    initial = f"{journey.domain['offer']} for {journey.domain['customer']}. {journey.domain['problem']}."
    preview = journey.request("idea", "/entry/chat/preview", {"text": initial, "request_id": "journey-" + uuid.uuid4().hex})
    proposal = preview.get("proposal", preview)
    journey.check(bool(proposal.get("id")), "idea", "working next step", str(preview))
    receipt = journey.request("idea", "/entry/chat/confirm", {"proposal_id": proposal.get("id")})
    journey.vid = receipt.get("venture_id")
    if not journey.vid:
        journey.finish()
        return
    journey.answers.append(initial)
    journey.work("idea", ["idea.clarify_turn"])
    reply = journey.chat("idea", "Show my idea")
    review = journey.action(reply, {"idea.review"})
    if review:
        reply = journey.tap("idea", reply, review)
    reply = journey.chat("clarification", journey.domain["pricing_hypothesis"], reply_to_question=True)
    journey.answered_fields.add("pricing_hypothesis")
    journey.work("clarification", ["idea.clarify_turn"])
    reply = journey.chat("clarification", "Show my idea")
    review = journey.action(reply, {"idea.review"})
    if review:
        reply = journey.tap("clarification", reply, review)
    reply = journey.chat("skip", "Skip the questions")
    journey.work("skip", ["idea.clarify_turn"])
    if not journey.action(reply, {"idea.confirm", "idea.proposal.approve"}) and review:
        reply = journey.tap("skip", reply, review)
    confirm = journey.action(reply, {"idea.confirm", "idea.proposal.approve"})
    journey.check(bool(confirm), "confirm", "working next step", str(reply))
    if confirm:
        reply = journey.tap("confirm", reply, confirm)
        journey.check(journey.steps[-1].get("saved_state", {}).get("idea") == "confirmed", "confirm",
            "reply agrees with saved state", "Confirm did not save a confirmed idea")
    reply = journey.chat("research", "Research my idea")
    from app.db.models import Job
    with journey.factory() as session:
        research_started = session.scalar(select(Job.id).where(Job.venture_id == journey.vid, Job.kind == "research.deep"))
    journey.check(bool(research_started) and reply.get("kind") != "confirmation",
        "research", "requested work starts without confirmation", str(reply))
    journey.check(not journey.action(reply, {"research.start"}), "research",
        "no second request for the same work", str(reply))
    journey.work("research", ["research.deep"])
    reply = journey.chat("research", "Show my research")
    journey.check(any(row["state"] in {"complete", "partial"} for row in journey.steps[-1].get("saved_state", {}).get("research", [])),
        "research", "working next step", "No completed research was saved")
    reply = journey.chat("experiment design", "Design an experiment to test whether people will pay")
    if journey.flag == "exploration_v1":
        reply = journey.chat("experiment design", "Explore options for a small demand test")
    journey.work("experiment design", ["experiment.exploration", "experiment.beliefs"])
    journey.check(bool(journey.steps[-1].get("saved_state", {}).get("experiments"))
        or reply.get("kind") == "exploration_options", "experiment design", "working next step", "No saved experiment design")
    reply = journey.chat("audience choice", "Who should I target?")
    route = journey.action(reply, {"gtm.route.confirm"})
    if route:
        reply = journey.tap("audience choice", reply, route)
    reply = journey.chat("audience choice", "Who should I reach out to?")
    choice = next((item for item in reply.get("actions", [])
        if item.get("action") == "chat.reply" and item.get("text", "").startswith("My customers are ")), None)
    journey.check(bool(choice), "audience choice", "working next step", "No selectable audience in " + str(reply))
    if choice:
        reply = journey.tap("audience choice", reply, choice)
    reply = journey.chat("customer search", "Find me customers")
    for attempt in range(5):
        action = journey.action(reply, {"gtm.find", "journey.jump_to", "gtm.route.confirm", "gtm.terms", "gtm.icp.select"})
        if not action:
            break
        reply = journey.tap("customer search", reply, action)
    journey.work("customer search", ["gtm.journey.prepare"])
    reply = journey.chat("customer search", "Find me customers")
    reply = journey.chat("drafts", "Show my drafts")
    from app.db.models import VentureDocument
    from app.autopilot import outreach

    with journey.factory() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == journey.vid,
            VentureDocument.kind == "gtm_journey"))
        saved = json.loads(row.provenance_json or "{}") if row else {}
    drafts = outreach.drafts(journey.vid)
    channel_drafts = [item["draft"] for item in saved.get("channels", []) + saved.get("need_signals", []) if item.get("draft")]
    journey.check(bool(drafts or channel_drafts),
        "drafts", "working next step", "Customer search did not save any reviewable draft")
    journey.check(not (bool(drafts or channel_drafts) and "No outreach drafts" in reply.get("body_md", "")),
        "drafts", "reply agrees with saved state", "You have saved drafts, but chat says no drafts are waiting")
    for draft in channel_drafts:
        journey.check(draft.strip() in reply.get("body_md", ""), "drafts",
            "saved draft is visible", "A saved research or community draft is missing from your reply")
    reply = journey.chat("asking to send", "Send the first message")
    journey.check(bool(journey.action(reply, {"gtm.send", "outreach.send", "connections.connect", "gtm.eligibility"}))
        or bool(re.search(r"connect|permission|consent|review|no.*draft|no.*people|no.*customer", reply.get("body_md", ""), re.I)),
        "asking to send", "requested sending or an actionable compliance blocker", str(reply))
    journey.finish()

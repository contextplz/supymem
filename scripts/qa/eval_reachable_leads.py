"""Local public-discovery evaluation; --self-test runs without network or a key."""
from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
import inspect
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
from unittest.mock import patch


def venture_json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("venture must be a JSON object")
    for field in ("name", "audience", "problem"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"{field} must be a nonempty string")
        value[field] = value[field].strip()
    if type(value.get("has_customers")) is not bool:
        raise ValueError("has_customers must be a boolean")
    if not isinstance(value.get("competitors"), list):
        raise ValueError("competitors must be a list of names or {name, domain} objects")
    for competitor in value["competitors"]:
        if isinstance(competitor, str) and competitor.strip():
            continue
        if (isinstance(competitor, dict) and isinstance(competitor.get("name"), str)
                and competitor["name"].strip()
                and (competitor.get("domain") is None or isinstance(competitor["domain"], str))):
            continue
        raise ValueError("each competitor needs a nonempty name and an optional string domain")
    return value


def debug_emit(stage, **values):
    from core.redact import redact_string
    secrets = [value for key, value in os.environ.items() if value and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD", "BASE_URL"))]
    emit("DEBUG", {"stage": stage, **{key: redact_string(str(value), exact_values=secrets)[:1500] for key, value in values.items()}})


class Exchanges:
    def __init__(self, file, *, replay=False):
        self.path, self.replay = Path(file), replay
        self.used = set()
        if replay:
            self.data = json.loads(self.path.read_text())
            if self.data.get("version") != 1 or not isinstance(self.data.get("exchanges"), list):
                raise ValueError("invalid replay file")
        else:
            if self.path.exists():
                raise ValueError("record file already exists; choose a new file")
            fields = ("LLM_PROVIDER", "LLM_PROFILE", "LLM_DEPLOYMENT_PRO", "LLM_DEPLOYMENT_FLASH", "LLM_CATALOG_MODEL", "MODEL_ROUTING_OVERRIDES", "LLM_REASONING_EFFORT")
            self.data = {"version": 1, "routing": {key: os.environ[key] for key in fields if key in os.environ}, "exchanges": []}
            self.save()

    def save(self):
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def send(self, original, client, request, **kwargs):
        import httpx
        from core.redact import redact_string
        endpoint = str(request.url.copy_with(query=None, username=None, password=None))
        fingerprint = hashlib.sha256(request.content if request.method != "GET" else str(request.url).encode()).hexdigest() if request.url.host in {"api.firecrawl.dev", "overpass-api.de", "npiregistry.cms.hhs.gov", "api.company-information.service.gov.uk"} else None
        legacy_fingerprints = {None, fingerprint}
        if self.replay and request.url.host == "api.firecrawl.dev" and request.content:
            body = json.loads(request.content)
            if endpoint.endswith("/scrape") and body.get("formats") == ["rawHtml"]:
                body["formats"] = ["markdown"]
                legacy_fingerprints.add(hashlib.sha256(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
            if endpoint.endswith("/search") and "excludeDomains" in body:
                body["excludeDomains"] = list(dict.fromkeys(domain for domain in body["excludeDomains"] if domain in {"linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com"}))
                legacy_fingerprints.add(hashlib.sha256(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
        stage, candidate_ids = None, None
        if endpoint.endswith(("/chat/completions", "/responses")):
            try:
                messages = json.loads(request.content).get("messages", [])
                instruction = next((row["content"] for row in messages if row.get("role") == "system"), "")
                stage = next((name for prefix, name in (("Plan at most eight", "plan"),
                    ("Score each reachable lead", "reachjudge"), ("Write a short personalised unsent note", "reachdraft"),
                    ("Write one draft per channel", "draft")) if instruction.startswith(prefix)), "judge")
                context = json.loads(messages[-1]["content"])
                if "candidates" in context:
                    candidate_ids = [row["id"] for row in context["candidates"]]
            except (ValueError, TypeError, KeyError, IndexError):
                pass
        if self.replay:
            for index, exchange in enumerate(self.data["exchanges"]):
                if index not in self.used and exchange["method"] == request.method and exchange["endpoint"] == endpoint and exchange.get("fingerprint") in legacy_fingerprints:
                    if stage and exchange.get("model_stage") not in {None, stage}:
                        continue
                    if "candidate_ids" in exchange and exchange["candidate_ids"] != candidate_ids:
                        continue
                    if stage and not exchange.get("model_stage"):
                        # ponytail: legacy empty replies have no stage; preserve their order until new recordings carry model_stage.
                        try:
                            result = json.loads(exchange["body"])["choices"][0]["message"]["content"]
                            value = json.loads(result)
                            kind = "plan" if "queries" in value else "draft" if any("body" in row for row in value.get("items", [])) else "judge" if any("kind" in row for row in value.get("items", [])) else None
                            if kind and kind not in stage:
                                continue
                        except (ValueError, TypeError, KeyError, IndexError):
                            pass
                    self.used.add(index)
                    if exchange.get("fingerprint") not in {None, fingerprint}:
                        emit("REPLAY_LIMITATION", {"endpoint": endpoint, "reason": "Legacy markdown/search recording; raw HTML and new exclusions were not recorded."})
                    if exchange.get("error"):
                        raise httpx.ConnectError("Recorded transport failure: " + exchange["error"], request=request)
                    return httpx.Response(exchange["status"], headers=exchange.get("headers", {}), content=exchange["body"].encode(), request=request)
            emit("REPLAY_LIMITATION", {"endpoint": endpoint, "reason": "No recorded exchange for this query or rate-limit retry; a fresh recording is required."})
            raise RuntimeError("replay_missing_exchange: " + request.method + " " + endpoint)
        exchange = {"method": request.method, "endpoint": endpoint, "fingerprint": fingerprint}
        if stage:
            exchange["model_stage"] = stage
            exchange["candidate_ids"] = candidate_ids
        try:
            response = await original(client, request, **kwargs)
            await response.aread()
            secrets = [value for key, value in os.environ.items() if value and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))]
            exchange.update(status=response.status_code, body=redact_string(response.text, exact_values=secrets))
            if response.headers.get("Retry-After"):
                exchange["headers"] = {"Retry-After": response.headers["Retry-After"]}
            return response
        except Exception as error:
            exchange["error"] = type(error).__name__
            raise
        finally:
            self.data["exchanges"].append(exchange)
            self.save()

    def install(self, patches):
        import httpx
        original = httpx.AsyncClient.send
        async def send(client, request, **kwargs):
            return await self.send(original, client, request, **kwargs)
        patches.enter_context(patch.object(httpx.AsyncClient, "send", send))
        if self.replay:
            routing = dict(self.data.get("routing", {}))
            model = next((row["endpoint"] for row in self.data["exchanges"] if row["endpoint"].endswith(("/chat/completions", "/responses"))), None)
            if model:
                routing["LLM_BASE_URL"] = model.rsplit("/", 2)[0] if model.endswith("/chat/completions") else model.rsplit("/", 1)[0]
            routing.update(LLM_API_KEY="replay-only", FIRECRAWL_API_KEY="replay-only", LLM_OFFLINE="0")
            patches.enter_context(patch.dict(os.environ, routing))
            patches.enter_context(patch("app.signals.research._pace_firecrawl", return_value=None))
            patches.enter_context(patch("app.signals.research._pace_structured", return_value=None))


class LocalRun:
    def __init__(self, journey):
        self.journey = journey
        self.doc = {"_runtime": {"calls_used": {}, "operations": {}, "ceilings": journey.CALL_CEILINGS | {"companies": journey.MAX_PROSPECTS, "contact_pages": 3}}}
        self.queries, self.responses, self.errors = [], {}, Counter()
        self.denied = Counter()
        from app.core.operation_receipts import OperationJournal
        self.journal = OperationJournal(read=lambda did: SimpleNamespace(id=did, workspace_id="local-eval", venture_id="local-eval"),
            mutate=self.mutate, document=self.document, before_call=lambda *args: None, prefix="gtm")

    def __getattr__(self, name):
        return getattr(self.journal, name)

    def document(self, unused):
        return json.loads(json.dumps(self.doc))

    def mutate(self, unused, fence, update):
        doc = self.document(unused)
        update(SimpleNamespace(id=unused, workspace_id="local-eval", venture_id="local-eval"), doc, None)
        self.doc = json.loads(json.dumps(doc))

    async def call(self, payload, bucket, identity, callback, *, meter=None, content_slots=0):
        key = (bucket, identity)
        if key in self.responses:
            return self.responses[key]
        counts, ceilings = self.doc["_runtime"]["calls_used"], self.doc["_runtime"]["ceilings"]
        contact = bucket.startswith("contact:")
        ceiling = ceilings["contact_pages"] if contact else ceilings[bucket]
        if ((contact and bucket not in counts and sum(name.startswith("contact:") for name in counts) >= ceilings["companies"])
                or counts.get(bucket, 0) >= ceiling):
            self.denied[bucket] += 1
            raise self.journey.FatalError("gtm_call_budget_exhausted")
        counts[bucket] = counts.get(bucket, 0) + 1
        if content_slots:
            if counts.get("signal_scrape", 0) + content_slots > ceilings["signal_scrape"]:
                raise self.journey.FatalError("gtm_call_budget_exhausted")
            counts["signal_scrape"] = counts.get("signal_scrape", 0) + content_slots
        if bucket in {"signal_search", "domain_search", "person_site_search", "hn"}:
            self.queries.append({"bucket": bucket, "query": identity})
        try:
            response = await callback()
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            self.errors[f"{bucket}:{type(error).__name__}" + (f":HTTP_{status}" if status else "")] += 1
            raise
        self.responses[key] = response
        import hashlib
        operation_key = bucket + "-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.doc["_runtime"]["operations"][operation_key] = {"state": "completed", "content_slots": content_slots}
        return response


class Rejections:
    """Observe real rejection branches, without substituting discovery logic."""
    def __init__(self, journey):
        self.journey, self.counts, self.qualification, self.local = journey, Counter(), {}, None
        self.lines = {}
        for function in (journey._discover, journey._qualify_signal):
            source, start = inspect.getsourcelines(function)
            tree = ast.parse(textwrap.dedent("".join(source)))
            branches = {}

            def visit(node, condition=""):
                if isinstance(node, ast.If):
                    condition = ast.unparse(node.test)
                if isinstance(node, ast.Continue) or (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is None):
                    branches[start + node.lineno - 1] = condition
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is None:
                    if any(isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == "result" for target in node.targets):
                        branches[start + node.lineno - 1] = "_same_offer"
                for child in ast.iter_child_nodes(node):
                    visit(child, condition)

            visit(tree)
            self.lines[function.__code__] = branches

    def trace(self, frame, event, argument):
        if frame.f_code not in self.lines:
            return None
        if event == "line" and frame.f_lineno in self.lines[frame.f_code]:
            condition = self.lines[frame.f_code][frame.f_lineno]
            values = frame.f_locals
            if frame.f_code is self.journey._qualify_signal.__code__:
                reason = ("missing_publication_date" if not values.get("published") else "stale_or_future_signal") if "published" in condition and ("timedelta" in condition or "not published" in condition) else (
                    "promotional_or_seller_text" if "clean" in condition else "listicle" if "title" in condition else "no_relevant_first_person_need")
                self.qualification[values["hit"]["url"]] = reason
                if self.local:
                    self.local.doc["_runtime"].setdefault("reachable_trace", []).append({"url": values["hit"]["url"],
                        "stage": "dropped", "reason": reason, "track": "need_signal"})
            else:
                if "judged" in condition and "channel" in condition:
                    return self.trace
                if "_verbatim" in condition and "quote" in condition:
                    return self.trace
                if condition == "consumer":
                    if values.get("signal") or values.get("channel"):
                        return self.trace
                    reason = self.qualification.get(values.get("url"), "no_qualified_need_or_channel")
                elif "not url" in condition:
                    reason = "invalid_or_blocked_url" if not values.get("url") else "duplicate_source"
                elif "_same_offer" in condition:
                    reason = "competitor_or_same_offer"
                elif "relevant_search_lead" in condition:
                    reason = "irrelevant_problem"
                elif "not signal or not affiliation" in condition:
                    reason = "no_explicit_company_affiliation" if values.get("signal") else self.qualification.get(values.get("url"), "unqualified_need")
                elif "competitor_switch" in condition:
                    reason = "switch_without_named_competitor"
                elif "not website" in condition:
                    reason = "no_unique_safe_company_domain"
                elif "not domain" in condition:
                    reason = "non_company_or_competitor_domain"
                else:
                    reason = "source_not_accessible"
                self.counts[reason] += 1
                if self.local:
                    self.local.doc["_runtime"].setdefault("reachable_trace", []).append({"url": values.get("url") or (values.get("hit") or {}).get("url", ""),
                        "stage": "dropped", "reason": reason, "track": "signal"})
        elif event == "return" and isinstance(argument, list) and frame.f_code is self.journey._discover.__code__:
            self.counts["outside_top_5_companies"] += max(0, len(frame.f_locals.get("grouped", {})) - 5)
        return self.trace


async def evaluate(venture, *, debug=False, exchanges=None):
    with ExitStack() as patches:
        if exchanges:
            exchanges.install(patches)
        patches.enter_context(patch("app.memory.working.get_working_memory", return_value={}))
        from app.gtm import journey
        from app.credits import gate
        if debug:
            discovery_model = journey._discovery_model
            async def debug_model(payload, stage, context):
                try:
                    return await discovery_model(payload, stage, context)
                except Exception as error:
                    current = error
                    for _ in range(4):
                        debug_emit(stage, batch=context.get("_batch"), exception=type(current).__name__,
                            reason=getattr(current, "reason", str(current)))
                        current = current.__cause__
                        if current is None:
                            break
                    raise
            patches.enter_context(patch.object(journey, "_discovery_model", debug_model))
            from llm_gateway import sole
            transmit = sole.transmit
            async def debug_transmit(**kwargs):
                stage = kwargs.get("purpose", "").removeprefix("gtm.discovery.")
                try:
                    result = await transmit(**kwargs)
                except Exception as error:
                    current = error
                    for _ in range(4):
                        debug_emit(stage, exception=type(current).__name__, reason=getattr(current, "reason", getattr(current, "code", type(current).__name__)))
                        current = current.__cause__
                        if current is None:
                            break
                    raise
                text = result.choices[0].message.content or ""
                debug_emit(stage, response=text)
                return result
            patches.enter_context(patch.object(sole, "transmit", debug_transmit))
        local, rejected = LocalRun(journey), Rejections(journey)
        rejected.local = local
        def trace(payload, url, stage, reason="", **facts):
            local.doc["_runtime"].setdefault("reachable_trace", []).append({"url": url, "stage": stage,
                **({"reason": reason} if reason else {}), **facts})
        patches.enter_context(patch("app.gtm.reachable.trace", side_effect=trace))
        if venture["has_customers"]:
            local.doc["customer_start"] = journey._customer_drafts(venture["name"], venture["audience"])
        patches.enter_context(patch.object(journey, "db", side_effect=RuntimeError("DB access is forbidden in this evaluation")))
        patches.enter_context(patch.object(journey, "_journal", return_value=local))
        patches.enter_context(patch.object(journey, "_signal_call", local.call))
        patches.enter_context(patch("app.llm.founder_provider.get_founder_config", return_value=None))
        patches.enter_context(patch("app.credits.accounts.get_account", return_value=SimpleNamespace(id="local-eval", scope="venture", workspace_id="local-eval", venture_id="local-eval")))
        def admit(**kwargs):
            fields = {key: value for key, value in kwargs.items() if key != "max_tokens"}
            return gate.Meter(**fields, passthrough=False, hold=SimpleNamespace(reservation_id="local-eval"), token_ceiling=kwargs.get("max_tokens", 0))
        patches.enter_context(patch.object(gate, "_enter", side_effect=admit))
        patches.enter_context(patch.object(gate, "_exit", return_value=None))
        patches.enter_context(patch("app.credits.reserve.protect_outcome", return_value=None))
        patches.enter_context(patch.object(gate, "reconcile_receipt", side_effect=lambda usage, binding: usage.get("actual_micro", 0)))
        patches.enter_context(patch("app.boundary.filter.record", return_value="local-eval"))
        patches.enter_context(patch("app.boundary.filter.audit", return_value=None))
        patches.enter_context(patch.dict(os.environ, {"GTM_ENRICH_ENABLED": "1"}))
        previous_trace = sys.gettrace()
        sys.settrace(rejected.trace)
        try:
            leads = await journey._discover(venture["audience"], problem=venture["problem"], competitors=venture["competitors"], offer=venture.get("offer", ""),
                payload={"workspace_id": "local-eval", "venture_id": "local-eval", "journey_id": "local-eval", "prepare_id": "local-eval", "_job_attempt": 1, "_worker_id": "local-eval"})
            failure = None
        except Exception as error:
            leads, failure = [], type(error).__name__
        finally:
            sys.settrace(previous_trace)
    return local, rejected, leads, failure


def emit(label, value):
    print(label, json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr))


def report(venture, local, rejected, leads, failure):
    emit("VENTURE", {"name": venture["name"], "mode": "B2C" if local.journey._consumer(venture["audience"]) else "B2B",
        "has_customers": venture["has_customers"], "scope": "public discovery and copy-only drafts; no DB, platform credits or outreach"})
    if local.doc.get("customer_start"):
        emit("CUSTOMER_START", local.doc["customer_start"])
    for query in local.queries:
        emit("QUERY", query)
    counts = local.doc["_runtime"]["calls_used"]
    emit("CALLS", {**{bucket: counts.get(bucket, 0) for bucket in local.journey.CALL_CEILINGS},
        "model_plan": counts.get("discovery-plan", 0), "model_judge": counts.get("discovery-judge", 0),
        "reachable_judges": sum(value for key, value in counts.items() if key.startswith("discovery-reachjudge-")),
        "reachable_drafts": sum(value for key, value in counts.items() if key.startswith("discovery-reachdraft")),
        "contact_pages": sum(count for bucket, count in counts.items() if bucket.startswith("contact:")),
        "per_company": {bucket.removeprefix("contact:"): count for bucket, count in counts.items() if bucket.startswith("contact:")}})
    emit("ACCESS", {"source_access": local.doc.get("source_access", "unavailable"), "failure": failure,
        "errors": dict(local.errors), "budget_denials": dict(local.denied)})
    if local.doc.get("discovery_plan"):
        emit("EXECUTED_PLAN", {"queries": local.doc["discovery_plan"]})
    keys = ["discovery-plan"] + sorted(key for key in local.doc["_runtime"]["operations"] if key.startswith(("discovery-judge", "discovery-reachjudge", "discovery-reachdraft")))
    for key in keys:
        operation = local.doc["_runtime"]["operations"].get(key, {})
        emit("MODEL", {"stage": key, "state": operation.get("state"), "error": operation.get("error_code"),
            "deployment": operation.get("usage", {}).get("model"), "queries": operation.get("response", {}).get("queries") if key == "discovery-plan" else None,
            "validation": operation.get("usage", {}).get("output", {})})
    for batch, reason in local.doc.get("_runtime", {}).get("judge_blocked_batches", {}).items():
        emit("GUARD", {"batch": batch, "reason": reason})
    for lead in leads:
        signal = next((item for item in lead["signals"] if item["why_now"] == lead["why_now"]), lead["signals"][0])
        person_mail = next((person.get("email") for person in lead.get("people", []) if person.get("name") == lead.get("person_name")), None)
        emit("B2B", {"company": lead["organization"], "domain": local.journey.enrich._host(lead["website"]), "why_now": lead["why_now"],
            **{field: signal.get(field) for field in ("quote", "source_url", "published_at", "confidence")},
            "person": {"name": lead.get("person_name"), "role": lead.get("person_role"), "source": lead.get("person_source_url")},
            "email": {"value": lead.get("email"), "status": "published_person_contact" if person_mail and person_mail == lead.get("email") else "published_company_contact" if lead.get("email") else "not_found",
                "contact_source": lead.get("contact_source_url"), "quote": lead.get("contact_quote")}})
    reachable = local.doc.get("reachable_leads", [])
    for lead in reachable:
        emit("REACHABLE", lead)
        emit("REACHABLE_LEAD", {field: lead.get(field) for field in ("id", "name", "role", "company", "value_exchange", "source_url", "quote", "routes", "outreach_hint", "registry", "works_at")})
        for route in lead["routes"]:
            if route["kind"] == "email":
                emit("EMAIL_SPOT_CHECK", {"name": lead["name"], "email": route["email"], "source_url": route["source_url"], "exact_snippet": route["quote"], "published_as": route.get("published_as", route["email"])})
    for event in local.doc["_runtime"].get("reachable_trace", []):
        emit("LEAD_TRACE", event)
    for candidate in local.doc["_runtime"].get("reachable_candidates_quality_v1", []):
        emit("REACHABLE_CANDIDATE", {"url": candidate["url"], "name": candidate["title"], "kind": candidate["kind"],
            "evidence_pages": candidate["pages"], "routes": candidate["routes"]})
    emit("REACHABLE_QUALITY", {"count": len(reachable), "email_or_form": sum(any(route["kind"] in {"email", "contact_form"} for route in lead["routes"]) for lead in reachable),
        "with_evidence": sum(bool(lead["quote"] and lead["source_url"]) for lead in reachable),
        "people_by_source": {source: sum(lead["routes"][0]["kind"] == source for lead in reachable) for source in ("linkedin", "x", "forum")},
        "people_with_value_exchange": sum(bool(lead.get("value_exchange")) for lead in reachable),
        "b2b_target_met": len(reachable) >= 10 and sum(any(route["kind"] in {"email", "contact_form"} for route in lead["routes"]) for lead in reachable) >= 5})
    candidates = local.doc["_runtime"].get("reachable_candidates_quality_v1", [])
    events = local.doc["_runtime"].get("reachable_trace", [])
    if len(reachable) < (5 if local.journey._consumer(venture["audience"]) else 10):
        emit("REACHABLE_SHORTFALL", {"qualified": len(reachable), "candidate_count": len(candidates),
            "unjudged_candidates_are_not_leads": len({event["url"] for event in events
                if event.get("reason") == "judge_omitted_candidate" and event.get("track") != "signal"}),
            "candidates_with_email_or_form": sum(any(route["kind"] in {"email", "contact_form"} for route in candidate["routes"]) for candidate in candidates),
            "drop_reasons": dict(Counter(event["reason"] for event in events if event["stage"] == "dropped" and event.get("reason"))),
            "replay_limit": "Replay freezes model judgments, including empty lists. New planner/judge instructions require a fresh recording; no leads are fabricated."})
    for channel in local.doc.get("channels", []):
        emit("CHANNEL", {field: channel.get(field) for field in ("name", "url", "source_url", "channel_role", "why_fits", "quote", "participation", "draft")})
    for signal in local.doc.get("need_signals", []):
        if not signal.get("company_evidence_url"):
            emit("ANONYMOUS_NEED", {"name": signal.get("name"), "quote": signal["quote"], "source": signal["source_url"], "date": signal["published_at"], "date_status": signal.get("date_status"), "recency": signal.get("recency")})
    emit("REJECTED", dict(sorted((reason, count) for reason, count in rejected.counts.items() if count)))


async def self_test():
    import httpx
    from datetime import datetime, timedelta, timezone

    venture = venture_json('{"name":"Eval","audience":"Independent bakery owners","problem":"bakery orders phone handling without manual spreadsheets","competitors":[{"name":"Vendor","domain":"vendor.example"}],"has_customers":false}')
    now = datetime.now(timezone.utc)
    anonymous = "We need bakery orders phone handling but I won't publish my employer."
    texts = {"fresh": "I work at [Acme Bakery](https://acme.example/).\nWe Independent bakery owners need bakery orders phone handling without manual spreadsheets.",
        "lookup": "I work at Lookup Bakery, and we Independent bakery owners need bakery orders phone handling without manual spreadsheets.",
        "stale": "I work at [Stale Bakery](https://stale.example/).\nWe need bakery orders phone handling.", "anonymous": anonymous,
        "vendor": "We offer our platform for bakery orders phone handling."}
    consumer = False
    reachable_mode = False
    omit_judge_ids = False
    signal_searches = 0

    def respond(request):
        nonlocal signal_searches
        if request.url.host == "model.example":
            body = json.loads(request.content)
            context = json.loads(body["messages"][-1]["content"])
            if "candidates" not in context:
                from app.gtm import journey
                answer = {"queries": ([{"query": "independent bakery Austin", "intent": "business"},
                    {"query": "independent bakery Boston", "intent": "business"},
                    {"query": "site:linkedin.com/in bakery Owner", "intent": "person"},
                    {"query": "site:x.com bakery orders phone", "intent": "person"},
                    {"query": "site:reddit.com/user baking", "intent": "person"}] if not consumer else [
                        {"query": "site:linkedin.com/in Swim Coach Bangalore", "intent": "person"},
                        {"query": "site:x.com Bangalore parents swimming", "intent": "person"},
                        {"query": "site:reddit.com/user parents swimming", "intent": "person"}]) if reachable_mode else [{"query": query, "intent": kind} for query, kind in zip(
                    journey._search_queries(context["audience"], context["problem"], context["competitors"]), journey.SIGNAL_INTENTS)]}
            else:
                import re
                from app.gtm import journey
                answer = {"items": []}
                for row in context["candidates"]:
                    if reachable_mode:
                        if "evidence_pages" in row:
                            page = row["evidence_pages"][0]
                            name = row["title"].split(" | ")[0]
                            quote = page["text"].splitlines()[0] if row["kind"] == "business" else page["text"]
                            answer["items"].append({"id": row["id"], "kind": row["kind"], "quote": quote, "affiliation": "", "name": name,
                                "url": page["url"], "role": "Owner" if name == "Alice Jones" else "Swim Coach" if name == "Raj Kumar" else "Designer" if name == "Alex Lee" else "",
                                "company": "", "location": "", "fit": 90, "why": "Published business workflow and customer type fit.",
                                "value_exchange": {"Alice Jones": "Owns an independent bakery and decides how phone orders are handled.",
                                    "Raj Kumar": "Teaches Bangalore parents' children to swim; serves the same customers as a local partner.",
                                    "Sam Rao": "Runs an independent bakery and publicly asks for help handling phone orders.",
                                    "Maya Rao": "Bangalore parent publicly asking for children's swimming lessons.",
                                    "baker": "Needs help managing bakery orders by phone.", "swim-parent": "Bangalore parent asking for children's swimming lessons."}.get(name, "")})
                        else:
                            answer["items"].append({"id": row["id"], "body": "Hi, I'm researching this workflow. Open to a 15-minute call? Reply with a time.",
                                "connection_note": "Hi, I'm exploring your workflow. Open to a 15-minute call? Reply with a time."})
                        continue
                    if context["mode"] == "B2C":
                        answer["items"].append({"id": row["id"], "kind": "channel", "quote": row["text"], "affiliation": "", "name": row["title"], "url": row["url"]})
                    for line in journey.enrich._clean(row["text"])[0].splitlines():
                        if re.search(r"\b(?:I|we)\b.*\bneed\b", line, re.I):
                            answer["items"].append({"id": row["id"], "kind": "customer_voice", "quote": line,
                                "affiliation": next((line for line in row["text"].splitlines() if "I work at" in line), ""), "name": "", "url": ""})
                if omit_judge_ids and "Score each reachable lead" in body["messages"][0]["content"] and len(context["candidates"]) > 1:
                    answer["items"] = answer["items"][:1]
            return httpx.Response(200, json={"id": "chatcmpl-synthetic", "object": "chat.completion", "created": int(now.timestamp()), "model": "DeepSeek-V4-Pro",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 200, "total_tokens": 500}})
        if request.url.host == "hn.algolia.com":
            if reachable_mode:
                return httpx.Response(200, json={"hits": []})
            return httpx.Response(200, json={"hits": [{"objectID": "123456", "title": "Bakery orders phone", "comment_text": anonymous,
                "created_at_i": int(now.timestamp()), "_tags": ["comment"]}]})
        body = json.loads(request.content)
        if request.url.path.endswith("/search"):
            if reachable_mode:
                assert "scrapeOptions" not in body
                if "linkedin" in body["query"]:
                    assert "linkedin.com" not in body["excludeDomains"]
                    rows = [{"url": "https://linkedin.com/in/alice", "title": "Raj Kumar | Swim Coach" if consumer else "Alice Jones | Owner",
                        "description": "Raj Kumar, Swim Coach teaching Bangalore parents' children to swim." if consumer else "Alice Jones, Owner of an independent bakery in Austin. I decide how we handle phone orders."},
                        {"url": "https://linkedin.com/in/alex", "title": "Alex Lee | Designer", "description": "Designer who enjoys bakery logos."}]
                elif "site:x.com" in body["query"]:
                    assert all(host not in body["excludeDomains"] for host in ("x.com", "twitter.com"))
                    rows = [{"url": "https://twitter.com/frontdesk", "title": "Maya Rao" if consumer else "Sam Rao",
                        "description": "I am a Bangalore parent looking for children's swimming lessons." if consumer else "I run an independent bakery and need help handling phone orders."},
                        {"url": "https://x.com/alex", "title": "Alex Lee | Designer", "description": "Designer who enjoys bakery logos."}]
                elif "reddit" in body["query"]:
                    rows = [{"url": "https://reddit.com/user/swim-parent" if consumer else "https://reddit.com/user/baker", "title": "swim-parent" if consumer else "baker",
                        "description": "I am a Bangalore parent looking for children's swimming lessons." if consumer else "I need help managing bakery orders by phone."}]
                else:
                    start = 0 if "Austin" in body["query"] else 5
                    rows = [{"url": f"https://business{index}.example/", "title": f"Business {index}", "description": "Independent bakery in Austin"} for index in range(start, start + 10)]
            elif "official website" in body["query"]:
                rows = [{"url": "https://lookup.example/", "title": "Lookup Bakery", "description": "Lookup Bakery official website"}]
            elif consumer:
                rows = [{"url": "https://forum.example/parents", "title": "Parents community forum", "description": "Parents community rules: no promotion. Parents need bakery orders phone handling."}]
            else:
                keys = list(texts)
                selected = keys[signal_searches % len(keys):] + keys[:signal_searches % len(keys)]
                signal_searches += 2
                rows = [{"url": "https://forum.example/" + key, "title": "Bakery orders phone", "description": texts[key]} for key in selected]
            return httpx.Response(200, json={"success": True, "data": {"web": rows}})
        url = body["url"]
        key = url.rsplit("/", 1)[-1]
        if "forum.example" in url:
            text = "Parents community rules: no promotion. Parents need bakery orders phone handling.\nI parents need bakery orders phone handling without manual spreadsheets." if consumer else texts[key]
            published = now - timedelta(days=366 if key == "stale" else 2)
            return httpx.Response(200, json={"success": True, "data": {"markdown": text, "metadata": {"publishedTime": published.isoformat()}}})
        host = httpx.URL(url).host
        assert not any(host == social or host.endswith("." + social) for social in ("linkedin.com", "reddit.com", "x.com", "twitter.com"))
        if reachable_mode:
            title = "Business " + host.removeprefix("business").split(".")[0]
            text = f"{title} is an independent bakery in Austin.\n[Contact](/contact) [Team](/team)" if key != "contact" else f"Contact {title}\nhello@{host}\nContact form: Send message"
            return httpx.Response(200, json={"success": True, "data": {"markdown": text, "metadata": {}}})
        text = (f"<address>Sam Roe, CEO\n<a href='mailto:sam@{host}'>Email Sam</a></address>" if key == "contact" else
            f"<p>Jane Doe, Founder</p><p>jane@{host}</p>" if key == "team" else "[Team](/team) [Contact](/contact)")
        return httpx.Response(200, json={"success": True, "data": {"markdown": text, "metadata": {}}})

    client = httpx.AsyncClient
    class SyntheticClient(client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
    with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "offline-self-test", "LLM_OFFLINE": "0", "LLM_PROVIDER": "azure_foundry", "LLM_PROFILE": "prod",
            "LLM_BASE_URL": "https://model.example/openai/v1", "LLM_API_KEY": "synthetic-model-key", "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro", "LLM_DEPLOYMENT_FLASH": "DeepSeek-V4-Pro"}), patch.object(httpx, "AsyncClient", SyntheticClient), patch("llm_gateway.provider_foundry.DefaultAsyncHttpxClient", SyntheticClient), patch("app.signals.research._pace_firecrawl", return_value=None):
        from contextlib import redirect_stdout
        from io import StringIO
        debug_output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            cassette = Path(directory) / "self-test.json"
            with redirect_stdout(debug_output):
                local, rejected, leads, failure = await evaluate(venture, debug=True, exchanges=Exchanges(cassette))
            recorded = json.loads(cassette.read_text())
            assert any(row["endpoint"].endswith("/chat/completions") for row in recorded["exchanges"])
            assert any(row["endpoint"].endswith("/search_by_date") for row in recorded["exchanges"])
            replay = Exchanges(cassette, replay=True)
            with patch.object(httpx.MockTransport, "handle_async_request", side_effect=AssertionError("Replay attempted network")):
                replayed, _, replay_leads, replay_failure = await evaluate(venture, exchanges=replay)
            assert replay_failure is None and {lead["organization"] for lead in replay_leads} == {lead["organization"] for lead in leads}
            assert len(replay.used) == len(recorded["exchanges"])
        debug_rows = [json.loads(line.split(" ", 1)[1]) for line in debug_output.getvalue().splitlines() if line.startswith("DEBUG ")]
        assert {row["stage"] for row in debug_rows if "response" in row} == {"plan", "judge"}, debug_rows
        assert all(len(row.get("response", "")) <= 1500 for row in debug_rows)
        assert failure is None, failure
        assert {lead["organization"] for lead in leads} == {"Acme Bakery", "Lookup Bakery"}, leads
        assert all(lead["email"].startswith("sam@") and lead["person_name"] == "Sam Roe" for lead in leads), leads
        assert all(not person.get("email") for lead in leads for person in lead["people"] if person["name"] == "Jane Doe")
        counts = local.doc["_runtime"]["calls_used"]
        assert counts["signal_search"] == 8 and counts["domain_search"] == 1 and counts["hn"] == 1
        assert counts["contact:acme.example"] == counts["contact:lookup.example"] == 3
        assert rejected.counts["stale_or_future_signal"] == 1 and rejected.counts["no_explicit_company_affiliation"] == 2, rejected.counts
        assert len([signal for signal in local.doc["need_signals"] if not signal.get("company_evidence_url")]) == 2
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            report(venture, local, rejected, leads, failure)
        rows = [line.split(" ", 1) for line in output.getvalue().splitlines()]
        assert sum(label == "QUERY" for label, raw in rows) == 10
        assert sum(label == "B2B" for label, raw in rows) == 2
        assert sum(label == "ANONYMOUS_NEED" for label, raw in rows) == 2
        assert all(isinstance(json.loads(raw), dict) for label, raw in rows)
        async def nothing():
            return "ok"
        assert await local.call({}, "signal_search", local.queries[0]["query"], nothing) != "ok"
        assert counts["signal_search"] == 8
        for bucket, ceiling in (("signal_search", 8), ("domain_search", 5), ("signal_scrape", 15), ("hn", 1), ("contact:acme.example", 3)):
            while counts.get(bucket, 0) < ceiling:
                await local.call({}, bucket, str(counts.get(bucket, 0)), nothing)
            try:
                await local.call({}, bucket, "over-budget", nothing)
            except local.journey.FatalError:
                pass
            else:
                raise AssertionError(bucket + " exceeded its ceiling")
        for host in [f"business{index}.example" for index in range(13)]:
            await local.call({}, "contact:" + host, "https://" + host, nothing)
        try:
            await local.call({}, "contact:sixteenth.example", "https://sixteenth.example", nothing)
        except local.journey.FatalError:
            pass
        else:
            raise AssertionError("company ceiling exceeded")
        consumer = True
        venture["audience"] = "Parents"
        local, rejected, leads, failure = await evaluate(venture)
        assert failure is None and not leads and len(local.doc["channels"]) == 1
        assert "no promotion" in local.doc["channels"][0]["participation"]
        assert local.doc["need_signals"] and not any(bucket.startswith("contact:") for bucket in local.doc["_runtime"]["calls_used"])
        reachable_mode, consumer = True, False
        venture["audience"] = "Independent bakery owners"
        with tempfile.TemporaryDirectory() as directory:
            cassette = Path(directory) / "reachable.json"
            local, _, _, failure = await evaluate(venture, exchanges=Exchanges(cassette))
            assert failure is None and len(local.doc.get("reachable_leads", [])) == 18, (failure, len(local.doc.get("reachable_leads", [])), local.doc["_runtime"]["calls_used"])
            counts = local.doc["_runtime"]["calls_used"]
            assert sum(count for key, count in counts.items() if key.startswith("contact:")) == 45
            assert all(count <= 3 for key, count in counts.items() if key.startswith("contact:"))
            assert all(lead["draft"] and lead["source_url"] and lead["quote"] for lead in local.doc["reachable_leads"])
            people = [lead for lead in local.doc["reachable_leads"] if lead["routes"][0]["kind"] in {"linkedin", "x", "forum"}]
            assert [lead["routes"][0]["kind"] for lead in people] == ["linkedin", "x", "forum"]
            assert all(lead["value_exchange"] for lead in people) and not any(lead["name"] == "Alex Lee" for lead in local.doc["reachable_leads"])
            assert all(lead["x_note"] and len(lead["x_note"]) <= 280 for lead in people)
            replay = Exchanges(cassette, replay=True)
            with patch.object(httpx.MockTransport, "handle_async_request", side_effect=AssertionError("Replay attempted network")):
                repeated, _, _, failure = await evaluate(venture, exchanges=replay)
            assert failure is None and repeated.doc["reachable_leads"] == local.doc["reachable_leads"]
            assert len(replay.used) == len(replay.data["exchanges"])
        omit_judge_ids = True
        with tempfile.TemporaryDirectory() as directory:
            cassette = Path(directory) / "omitted.json"
            partial, _, _, failure = await evaluate(venture, exchanges=Exchanges(cassette))
            assert failure is None
            runtime = partial.doc["_runtime"]
            assert runtime["calls_used"]["discovery-reachjudge"] == 5
            retries = [key for key in runtime["operations"] if key.startswith("discovery-reachjudge-") and key.endswith("-omitted")]
            assert retries and all(runtime["operations"][key]["state"] == "completed" for key in retries)
            replay = Exchanges(cassette, replay=True)
            with patch.object(httpx.MockTransport, "handle_async_request", side_effect=AssertionError("Replay attempted network")):
                repeated, _, _, failure = await evaluate(venture, exchanges=replay)
            assert failure is None and repeated.doc["reachable_leads"] == partial.doc["reachable_leads"]
            assert len(replay.used) == len(replay.data["exchanges"])
        omit_judge_ids = False
        consumer = True
        venture["audience"] = "Bangalore parents"
        venture["problem"] = "Find children's swimming lessons in Bangalore"
        local, _, _, failure = await evaluate(venture)
        assert failure is None and {lead["name"] for lead in local.doc["reachable_leads"]} == {"Raj Kumar", "Maya Rao", "swim-parent"}
        assert not any(bucket.startswith("contact:") for bucket in local.doc["_runtime"]["calls_used"])
    print("self-test OK: reachable B2B/B2C, omitted-id retries, judge budgets, LinkedIn/X/forum priority, mutual value excludes weak profiles, social fetch bans, published routes, drafts, record/replay and all ceilings; no network")


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / "services/company_os"), str(root / "packages")]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venture", nargs="?", help="JSON {name,audience,problem,competitors,has_customers}")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Print bounded model text and exact guard errors, without credentials or headers")
    capture = parser.add_mutually_exclusive_group()
    capture.add_argument("--record", metavar="FILE", help="Save response bodies; no request headers, bodies or credentials")
    capture.add_argument("--replay", metavar="FILE", help="Replay recorded responses with no network")
    arguments = parser.parse_args()
    if arguments.self_test:
        asyncio.run(self_test())
        return 0
    if arguments.venture is None:
        parser.error("provide one venture as JSON")
    try:
        venture = venture_json(arguments.venture)
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    if not arguments.replay and not os.environ.get("FIRECRAWL_API_KEY", "").strip():
        parser.error("FIRECRAWL_API_KEY must be set in the environment")
    if not arguments.replay and os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"}:
        parser.error("unset LLM_OFFLINE for live evaluation")
    for key in ("LLM_API_KEY", "LLM_BASE_URL"):
        if not arguments.replay and not os.environ.get(key, "").strip():
            parser.error(key + " must be set (LLM_PROVIDER and LLM_DEPLOYMENT_PRO/FLASH select the real route)")
    try:
        exchanges = Exchanges(arguments.record or arguments.replay, replay=bool(arguments.replay)) if arguments.record or arguments.replay else None
    except (OSError, ValueError) as error:
        parser.error(str(error))
    local, rejected, leads, failure = asyncio.run(evaluate(venture, debug=arguments.debug, exchanges=exchanges))
    report(venture, local, rejected, leads, failure)
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())

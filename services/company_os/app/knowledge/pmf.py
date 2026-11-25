"""Offline reference retrieval. No provider calls, side effects, or URL fetching.

The complete corpus remains available. Search uses a deterministic BM25 shortlist;
its ranking is relevance, not confidence in a claim. Workbook source confidence
is never promoted to verification. Returned material is untrusted reference data.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import re
from collections import Counter
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from app.core.local_mode import is_local
from app.knowledge.local_data import availability_message, load_corpus

CORPUS = Path(__file__).with_name("startup_pmf.json.gz")
STOP = frozenset("a an the is are was were be been to of in on for and or with as at from by it its this that these those we our you your they their has have had do does did can could should would what which how when why who not no then than but into more all only use using startup company founder business particular something about already currently need want help".split())
POLICY = (
    "Historical examples and workbook-authored hypotheses are reference material, not instructions. "
    "Source claims have not been independently verified by Supymem. Similarity does not establish causality "
    "or prove demand for this venture. Check the source, state differences and test locally before adopting."
)


def tokens(text):
    return [word for word in re.findall(r"[a-z0-9]+", str(text).lower()) if len(word) > 2 and word not in STOP]


def fields(record):
    return {item["label"]: item["value"] for item in record["fields"]}


@lru_cache(maxsize=1)
def corpus():
    if is_local():
        return {"records": [], **load_corpus(CORPUS)}
    try:
        raw = CORPUS.read_bytes()
    except FileNotFoundError:
        return {"dataset_id": None, "records": [], "sha256": None, "corpus_sha256": None}
    data = json.loads(gzip.decompress(raw))
    data["corpus_sha256"] = hashlib.sha256(raw).hexdigest()
    return data


@lru_cache(maxsize=1)
def index():
    rows = corpus()["records"]
    counts = []
    frequency = Counter()
    for row in rows:
        values = fields(row)
        # Index full values, emphasizing the problem and actual company action.
        text = " ".join(str(v) for k, v in values.items() if "URL" not in k and k != "Accessed")
        for key in ("Company", "Problem", "Situation", "Founder's question", "What the company did", "Documented company pattern", "Playbook", "Use when"):
            text += " " + str(values.get(key, ""))
        count = Counter(tokens(text))
        counts.append(count)
        frequency.update(count.keys())
    average = sum(sum(count.values()) for count in counts) / max(len(rows), 1)
    return rows, counts, frequency, average


def safe_urls(record):
    found = []
    for item in record["fields"]:
        if item["label"] not in {"Source URL", "Source URLs", "URL", "Company Website"}:
            continue
        for value in re.findall(r"https?://[^\s<>\";]+", str(item["value"])):
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if valid and value not in found:
                found.append(value)
    return found


def card(record, matched=()):
    value = fields(record)
    return {**record, "company": value.get("Company"),
            "title": value.get("Company") or value.get("Playbook") or value.get("Situation") or value.get("Decision theory") or record["sheet"],
            "documented_action": value.get("What the company did") or value.get("Documented company pattern"),
            "problem": value.get("Problem") or value.get("Founder's question") or value.get("Situation") or value.get("Use when"),
            "adaptation": value.get("Transfer theory") or value.get("Reusable playbook") or value.get("Premise"),
            "why_it_worked": value.get("Why it worked") or value.get("Transfer theory"),
            "experiment": value.get("Reusable experiment") or value.get("Exact experiment") or value.get("Smallest useful test") or value.get("Exact steps"),
            "metric": value.get("Primary metric") or value.get("Primary signal"),
            "stop_condition": value.get("Stop / kill rule") or value.get("Failure mode"),
            "boundary": value.get("Evidence boundary") or value.get("Evidence status") or POLICY,
            "limitations": value.get("When not to copy") or value.get("Limits and guardrails") or value.get("Do not copy blindly") or value.get("Risk guardrail"),
            "source_confidence": value.get("Confidence") or value.get("Evidence strength"),
            "source_urls": safe_urls(record), "matched_terms": sorted(matched), "policy": POLICY}


def get_record(record_id):
    row = next((row for row in corpus()["records"] if row["id"] == record_id), None)
    return card(row) if row else None


def search(query, *, limit=6, kind=None, stage=None, exact_kind=False):
    words = set(tokens(query[:6000])[:100])
    if not words:
        return []
    rows, counts, frequency, average = index()
    candidates = []
    for row, count in zip(rows, counts):
        if kind and (row["kind"] != kind if exact_kind else not row["kind"].startswith(kind)):
            continue
        values = fields(row)
        row_stage = str(values.get("Stage") or values.get("Lifecycle stage") or "")
        matched = words.intersection(count)
        if not matched:
            continue
        length = sum(count.values())
        score = sum(math.log(1 + (len(rows) - frequency[word] + .5)/(frequency[word] + .5))
                    * count[word] * 2.2 / (count[word] + 1.2 * (.25 + .75 * length / average)) for word in matched)
        if stage and stage.casefold() in row_stage.casefold():
            score += 12
        candidates.append((score, row, matched))
    candidates.sort(key=lambda item: (-item[0], item[1]["id"]))
    result, companies = [], set()
    for score, row, matched in candidates:
        company = fields(row).get("Company")
        if company and str(company).lower() in companies:
            continue
        result.append({**card(row, matched), "relevance_score": round(score, 4)})
        if company:
            companies.add(str(company).lower())
        if len(result) >= max(1, min(limit, 20)):
            break
    return result


# These are retrieval tags, not claims about the historical companies.  A tag
# must occur in both the founder's context and an explicit workbook field; a
# generic word buried in a transfer theory cannot establish a domain match.
FACETS = {
    "AI": r"\b(?:ai|artificial intelligence|llm|agents?)\b",
    "security": r"\b(?:security|cybersecurity|identity|fraud|compliance)\b",
    "developer tooling": r"\b(?:developer|developers|api|devtools?|software engineer)\b",
    "enterprise": r"\b(?:enterprise|large companies|corporate|procurement)\b",
    "SMB": r"\b(?:smb|small businesses?|small companies)\b",
    "consumer": r"\b(?:consumer|b2c|individual users?)\b",
    "B2B": r"\b(?:b2b|businesses|companies|teams)\b",
    "SaaS": r"\b(?:saas|subscription software|cloud software)\b",
    "marketplace": r"\b(?:marketplace|two.sided|buyers? and sellers?)\b",
    "idea validation": r"\b(?:idea|problem discovery|problem validation|customer discovery|market validation)\b",
    "sales": r"\b(?:sales|selling|sell|leads?|pipeline|closing|conversion)\b",
    "pricing": r"\b(?:pricing|price|willingness to pay|monetiz|revenue)\b",
    "positioning": r"\b(?:positioning|messaging|differentiation|category)\b",
}
FACET_WEIGHT = {"security": 5, "AI": 4, "developer tooling": 4, "enterprise": 3,
                "SMB": 3, "consumer": 3, "B2B": 2, "SaaS": 2, "marketplace": 3,
                "idea validation": 2, "sales": 3, "pricing": 3, "positioning": 3}
GENERIC_CONTEXT = frozenset({"idea", "validation", "problem", "business", "startup",
                             "stage", "company", "customer", "market", "early", "sales"})


def _facets(text):
    return {name for name, pattern in FACETS.items() if re.search(pattern, text, re.I)}


def _context_facets(text):
    """Do not mistake a buyer's occupation or a required API key for a market."""
    found = _facets(text)
    if "developer tooling" in found and not re.search(
        r"\b(?:devtools?|developer (?:tools?|platforms?|teams?|workflows?|collaboration)"
        r"|developers? (?:build|integrat|tools?|apis?)|api(?:s)?-first"
        r"|developers?\b[^.!?;]*\b(?:connect|integrat\w*)\b[^.!?;]*\bapis?\b"
        r"|api(?:s)? (?:platform|tooling))\b", text, re.I
    ):
        found.remove("developer tooling")
    return found


def _context_text(query):
    """Keep structured idea values, while allowing ordinary decision text."""
    try:
        value = json.loads(query)
    except (ValueError, TypeError):
        return str(query)
    if isinstance(value, (dict, list)):
        def strings(node):
            if isinstance(node, dict):
                for item in node.values():
                    yield from strings(item)
            elif isinstance(node, list):
                for item in node:
                    yield from strings(item)
            elif isinstance(node, (str, int, float)):
                yield str(node)
        return " ".join(strings(value))[:6000]
    return str(query)


def reference_context(raw_idea: str, structured: dict) -> str:
    """Use founder intent and core offer, excluding incidental requirements."""
    fields = ("summary", "problem", "customer", "offer", "workflow", "pricing_hypothesis")
    context = {"founder_idea": raw_idea[:3000]}
    context.update({key: structured[key] for key in fields if isinstance(structured.get(key), str)})
    return json.dumps(context, ensure_ascii=False)


def reference_reason(context, evidence):
    """Require a specific domain or an uncommon shared phrase in source fields."""
    context = _context_text(context)
    desired = _context_facets(context) & {"security", "developer tooling", "marketplace"}
    rows, _, frequency, _ = index()
    words = tokens(context)
    # shortcut: lexical evidence misses paraphrases, upgrade only with source-backed semantic matching.
    distinctive = {word for word in words if word not in GENERIC_CONTEXT
                   and frequency[word] <= max(2, len(rows) * .05)}
    phrases = {(left, right) for left, right in pairwise(words)
               if left in distinctive and right in distinctive}
    for value in evidence:
        for sentence in re.split(r"(?<=[.!?])\s+", str(value or "")):
            source_words = tokens(sentence)
            shared = _facets(" ".join(word for word in source_words if word in words))
            shared -= {"AI", "B2B", "SaaS", "SMB", "consumer", "idea validation"}
            if not (desired & _facets(sentence)) and len(shared) < 2 and not phrases.intersection(pairwise(source_words)):
                continue
            return f'Your context overlaps with this source: "{sentence}". Check the source before applying it.'
    return None


def recommend(query, *, limit=3, kind="historical_claim", stage=None, exact_kind=False):
    """Rank workbook precedents by context and explain fit without inventing facts.

    This remains read-only retrieval. A broad BM25 shortlist is filtered on
    explicit workbook model/sector/stage/problem fields; generic similarities
    alone do not earn a founder-facing recommendation.
    """
    context = _context_text(query)
    desired = _context_facets(context)
    ranked = []
    for row in search(context, limit=20, kind=kind, exact_kind=exact_kind):
        value = fields(row)
        explicit = " ".join(str(value.get(key, "")) for key in (
            "Sector", "Model", "Stage", "Lifecycle stage", "Problem family",
            "Problem", "Founder's question", "Situation", "Use when",
            "What the company did", "Documented company pattern"))
        matched_facets = desired & _facets(explicit)
        domain_facets = desired & {"AI", "security", "developer tooling", "marketplace"}
        if domain_facets and not (matched_facets & domain_facets):
            continue
        reason = reference_reason(context, (value.get(key) for key in (
            "Sector", "Model", "Playbook", "Problem family", "Problem", "Founder's question",
            "Situation", "Use when", "What the company did", "Documented company pattern")))
        if not reason:
            continue
        score = row["relevance_score"] + sum(FACET_WEIGHT[facet] for facet in matched_facets)
        row_stage = str(value.get("Stage") or value.get("Lifecycle stage") or "")
        if stage and stage.casefold() in row_stage.casefold():
            score += 12
        labels = sorted(matched_facets, key=lambda name: (-FACET_WEIGHT[name], name))
        boundaries = [name for name in ("security", "AI", "developer tooling", "enterprise", "SMB", "consumer", "B2B", "SaaS", "marketplace")
                      if name in desired and name not in _facets(explicit)]
        difference = ("The workbook does not establish a match on " + ", ".join(boundaries[:3]) + ". "
                      if boundaries else "The same customer constraints and outcome have not been established here. ")
        difference += "Check the original source and test this in your venture before adopting it."
        next_action = (row.get("experiment") or value.get("First action") or
                       value.get("First 48-hour action") or "Compare the case with your customer evidence and choose one bounded test.")
        ranked.append((score, row["id"], {**row, "relevance_reason": reason,
                                          "important_difference": difference,
                                          "suggested_next_action": str(next_action)[:800],
                                          "fit_labels": labels}))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    result, companies = [], set()
    for _, _, row in ranked:
        company = str(row.get("company") or "").casefold()
        if company and company in companies:
            continue
        result.append(row)
        if company:
            companies.add(company)
        if len(result) >= max(1, min(limit, 20)):
            break
    return result


def planning_context(idea, *, raw_idea="", stage="MVP and delivery"):
    query = reference_context(raw_idea, idea)
    cases = recommend(query, limit=3, kind="historical_claim", stage=stage)
    guidance = search(query, limit=2, kind="playbook_guidance", stage=stage)
    # Weak one-word coincidences may be browsed, but don't enter paid decisions.
    selected = [row for row in cases + guidance if len(row["matched_terms"]) >= 2]
    data = corpus()
    return {"dataset_id": data["dataset_id"], "corpus_sha256": data["corpus_sha256"],
            "policy": POLICY, "items": selected, **availability_message(data)}


def idea_references(structured):
    """Optional, offline display metadata for a summary and its approval reply."""
    from app.knowledge import yc_video_advice

    if structured.get("project_intent") == "personal":
        return []
    query = reference_context("", structured)
    startups, advice = [], []
    try:
        for row in recommend(query, limit=4, kind="historical_claim", exact_kind=True):
            value = fields(row)
            outcome, lesson = value.get("Why it worked"), value.get("Reusable playbook")
            if not (row.get("company") and row.get("documented_action") and outcome and lesson and row.get("source_urls")):
                continue
            startups.append({"id": row["id"], "name": row["company"],
                             "what_they_did": row["documented_action"], "outcome": outcome,
                             "lesson": lesson, "url": row["source_urls"][0],
                             "relevance_reason": row["relevance_reason"],
                             "important_difference": row["important_difference"]})
    except Exception:
        logging.getLogger(__name__).warning("Idea case references unavailable", exc_info=True)
    try:
        for row in yc_video_advice.recommend(_context_text(query), limit=2, stage="idea"):
            video = row.get("source_video") or {}
            if row.get("title") and row.get("action_discussed") and video.get("url"):
                advice.append({"id": row["id"], "title": row["title"],
                               "line": row["action_discussed"], "url": video["url"]})
    except Exception:
        logging.getLogger(__name__).warning("Idea YC references unavailable", exc_info=True)
    if len(startups) < 2:
        startups = []
    if not startups and not advice:
        return []
    return [{"founder_references": {"startups": startups, "yc_advice": advice}}]


def journey_stage(next_step, run_status=None):
    """Project the existing journey receipt, not the founder's aspirational stage."""
    step = next_step or {}
    status = run_status or {}
    if step.get("has_customers") is True:
        return "Distribution & growth"
    key = str(step.get("key") or "")
    if key.startswith("idea.clarify"):
        return "Problem discovery"
    if key.startswith(("research.", "idea.valid", "idea.review_validation")):
        return "Segment validation"
    if key.startswith(("plan.", "site.", "build.")):
        return "MVP and delivery"
    text = " ".join(str(value or "") for value in (
        step.get("key"), step.get("title"), step.get("detail"), step.get("href"),
        status.get("stage"), status.get("activity"), (status.get("action") or {}).get("href")))
    if re.search(r"\b(pricing|economics|willingness to pay)\b", text, re.I):
        return "Pricing and economics"
    if re.search(r"\b(distribution|growth)\b", text, re.I):
        return "Distribution & growth"
    if re.search(r"\b(replies|feedback|learning|learn|retention|activation|experiment)\b", text, re.I):
        return "Activation & retention"
    if re.search(r"\b(gtm|customers|outreach|sales)\b", text, re.I):
        return "Founder-led sales & outreach"
    if re.search(r"\b(plan|planning|build|building|site|page|publish|product)\b", text, re.I):
        return "MVP and delivery"
    if re.search(r"\b(research|validation|validating|segment)\b", text, re.I):
        return "Segment validation"
    return "Problem discovery"


STAGE_ACTIONS = {
    "Problem discovery": ("Ask one recent user of the workaround to show the last time this problem occurred", "Repeated pain evidence"),
    "Segment validation": ("Compare one narrow customer segment's workaround and ask for a concrete commitment", "Qualified commitments"),
    "MVP and delivery": ("Test the smallest usable version of the offer with one target customer before adding scope", "Completed core workflow"),
    "Pricing and economics": ("Offer a bounded paid pilot and record the customer's commitment, not a hypothetical price preference", "Paid commitments and delivery cost"),
    "Founder-led sales & outreach": ("Draft a personal problem-focused message to one qualified prospect and review it before sending", "Qualified replies and pilot commitments"),
    "Activation & retention": ("Review a recent reply or failed first-use attempt and test one change to the recurring workflow", "Repeat use and activation"),
    "Distribution & growth": ("Test one repeatable acquisition channel with a small cohort before scaling spend", "Retained customers per channel"),
}


def coaching_card(row, query, step, stage):
    value = fields(row)
    try:
        idea = json.loads(query)
    except (ValueError, TypeError):
        idea = {}
    customer = idea.get("customer") if isinstance(idea, dict) else None
    action, _ = STAGE_ACTIONS[stage]
    # ponytail: deterministic step/customer templates, metered tailoring only if these prove too generic.
    action = f"For {step.get('title') or stage}: {action}{' with ' + customer[:180] if isinstance(customer, str) and customer else ''}."
    experiment = row.get("experiment") or value.get("Reusable playbook")
    if experiment:
        action += f" Adapt this workbook test: {str(experiment)[:600]}"
    case_specific = ("When not to copy", "Limits and guardrails", "Do not copy blindly", "Risk guardrail",
                     "Primary metric", "Primary signal")
    repeated = {key for key in case_specific if value.get(key) and any(
        other["id"] != row["id"] and fields(other).get("Company") not in {None, row.get("company")}
        and fields(other).get(key) == value[key] for other in corpus()["records"])}
    limits = [str(value[key]) for key in case_specific[:4] if value.get(key) and key not in repeated]
    urls = row.get("source_urls") or []
    title = value.get("Source title") or value.get("Source organization") or row["title"]
    sources = [{"title": str(title), "url": url} for url in urls]
    return {"id": row["id"], "company": row.get("company") or row["title"], "stage": stage,
            "situation_at_this_stage": row.get("problem") or row.get("relevance_reason"),
            "what_they_did": row.get("documented_action"),
            "why_it_worked": value.get("Why it worked"),
            "do": action, "avoid": "Avoid: " + " ".join(dict.fromkeys(limits)) if limits else None,
            "metric": next((value[key] for key in case_specific[4:] if value.get(key) and key not in repeated), None),
            "sources": sources,
            "honesty": "Their result isn't proof yours will work, test it."}


def coaching(query, *, next_step=None, run_status=None, limit=6):
    from app.knowledge import yc_video_advice

    stage = journey_stage(next_step, run_status)
    items = [coaching_card(row, query, next_step or {}, stage)
             for row in recommend(query, stage=stage, limit=limit)
             if not (next_step or {}).get("has_customers") or any(
                 str(fields(row).get(key) or "").casefold() in {"distribution & growth", "growth", "early traction"}
                 for key in ("Stage", "Lifecycle stage"))
             if row.get("company") and row.get("documented_action")]
    quote = yc_video_advice.quote(query, context=query, topic=(next_step or {}).get("title") or stage)
    if items and quote:
        items[0]["yc_says"] = quote
    data = corpus()
    return {"stage": stage, "items": items, "abstained": not items,
            "dataset_id": data["dataset_id"], "corpus_sha256": data["corpus_sha256"],
            **availability_message(data, yc_video_advice.corpus())}

from fastapi import HTTPException
from sqlalchemy import select
from app.db.models import Venture
from app.db.supymem_models import IdeaRevision, IdeaSession
from app.knowledge import pmf
from app.state import db

def knowledge_venture_query(venture_id, workspace_id):
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.workspace_id != workspace_id:
            raise HTTPException(404)
        q = venture.raw_idea or venture.name
        idea = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == workspace_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1))
        revision = session.get(IdeaRevision, idea.current_revision_id or idea.confirmed_revision_id) if idea and (idea.current_revision_id or idea.confirmed_revision_id) else None
        if revision and (revision.workspace_id, revision.venture_id, revision.session_id) == (workspace_id, venture_id, idea.id):
            q = pmf.reference_context(q, json.loads(revision.structured or "{}"))
    return q

def knowledge_reply_count(venture_id, workspace_id):
    from sqlalchemy import func
    from app.db.models import GtmTouch
    with db()() as session:
        replies = session.scalar(select(func.count()).select_from(GtmTouch).where(
            GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id,
            GtmTouch.direction == "in", GtmTouch.simulated == 0,
            (GtmTouch.classification != "automated") | GtmTouch.classification.is_(None)))

    return replies


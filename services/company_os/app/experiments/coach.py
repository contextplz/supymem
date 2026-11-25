"""Grounded interview coaching and private alternatives to blocked tests."""
from __future__ import annotations

import json
import re

from app.experiments import ledger
from core.copy import founder_copy

_INTERVIEW = re.compile(
    r"\b(interview(?:ed|s)?|(?:customer|discovery|user|sales) calls?|"
    r"(?:spoke|talked) (?:to|with)|(?:had|finished|completed) (?:a |the )?call)\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(would|could|might|will|won['’]?t|maybe|if|someday|intend(?:ed)?|plan(?:ned)? to|"
                           r"(?:they|he|she|we|I)['’](?:d|ll))\b", re.I)
_LEADING = re.compile(r"\b(don'?t you|wouldn'?t you|isn'?t it|surely|you agree|do you like|did you like)\b", re.I)
# shortcut: English commitment clauses only, use structured question intents for other languages.
_COMMITMENT_QUESTION = re.compile(
    r"^(?:(?:would|could|can|will|do)\s+you\s+(?:agree(?:\s+in\s+principle)?(?:\s+to\b|\s*\?$)|"
    r"(?:opt[ -]?in|consent|approve|authorize)\b|"
    r"(?:send|share|provide|write|allow|permit)\b(?=[\s\S]*\b(?:written|outline|confirmation|agreement|access)\b)|introduce\b|"
    r"(?:book|schedule|reserve|pay|give|spend)\b(?=[\s\S]*\b(?:session|meeting|pilot|trial|deposit|minutes?|hours?)\b))|"
    r"(?:what|which|who|how)\b(?=[\s\S]*\b(?:approv\w*|permissions?|consent)\b)|"
    r"(?:when|what\s+date|which\s+date)\b(?=[\s\S]*\b(?:start|meet|begin|schedule|available)\b))", re.IGNORECASE)
_INTERVIEW_FALLBACK = ("What happened the last time you faced it?", "What did you try that time?",
    "What did that incident cost you?", "What happened when you tried to change it?")
_PRAISE = re.compile(r"\b(love[ds]?|great|cool|interesting|brilliant|nice idea|sounds? good)\b", re.I)
_FACT = re.compile(r"\b(spent|spend|paid|pay|used|use|tried|bought|purchased|built|hired|lost|wasted|switched)\b", re.I)
_NEGATED = re.compile(r"\b(not|never|no|haven'?t|didn'?t|did not|hasn'?t|refused|declined|cancelled|canceled)\b", re.I)
_UNHELD = re.compile(r"\b(?:calls?|interviews?) (?:are |is |were |was )?(?:blocked|cancelled|canceled|not held)\b|"
                     r"\b(?:haven'?t|have not|couldn'?t|could not|unable to)\b.{0,40}"
                     r"\b(?:interview(?:ed)?|call(?:ed)?|speak|talk)\b", re.I)
_RESOURCES = (
    ("money", re.compile(r"\b(pre-?ordered|signed.{0,35}(?:contract|purchase order)|"
                         r"(?:commit(?:ted)?|agreed)\s+to.{0,45}(?:pre-?order|purchase|deposit)|"
                         r"(?:paid|sent|transferred).{0,40}(?:deposit|prepayment|to us|us\b))\b", re.I)),
    ("reputation", re.compile(r"\b(introduced|offered to introduce|made.{0,20}introduction|sent.{0,20}introduction|"
                              r"shared.{0,35}(?:colleague|manager|boss).{0,20}contact)\b", re.I)),
    ("time", re.compile(r"\b((?:booked|scheduled|reserved).{0,30}(?:call|demo|meeting|session|pilot)|"
                        r"agreed to.{0,20}(?:spend|give).{0,15}\d+ (?:minutes|hours)|"
                        r"agreed to.{0,25}(?:pilot|trial|test).{0,30}"
                        r"(?:tomorrow|next week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", re.I)),
)
_BLOCKERS = (
    ("budget", re.compile(r"\b(?:budget|credits?|allowance) (?:is |are |was |has been )?(?:exhausted|used up|depleted)|"
                          r"\b(?:out of|no|not enough) (?:credits?|budget)|\bran out of (?:credits?|budget)\b", re.I)),
    ("connection", re.compile(r"\b(?:missing|no|without) (?:a |the )?connection|\bnot connected|"
                              r"\b(?:gmail|account|provider) (?:is |was )?disconnected\b", re.I)),
    ("platform", re.compile(r"\b(?:platform|account|provider|community|moderator|post|ads?|advert|advertisement)"
                            r".{0,35}\b(?:refused|rejected|blocked|denied|banned)|"
                            r"\b(?:refused|rejected|blocked|denied|banned).{0,35}"
                            r"\b(?:platform|post|ads?|advert|advertisement)\b", re.I)),
    ("traffic", re.compile(r"\b(?:no|zero|not enough|without) (?:traffic|visitors?|views)|"
                           r"\bnobody (?:visited|saw the page)\b", re.I)),
)


def _question_parts(text: str) -> tuple[str, str]:
    # shortcut: English question clauses, use structured question spans for other languages.
    question_pattern = (r"\b(?:what|how|when|where|why|which|who|tell me|describe|walk me through|"
                        r"did|do|does|have|has|can|could|would|will|is|are|if)\b")
    start = (re.match(r"[\s\"'“”‘’]*(?P<question>" + question_pattern + ")", text, re.IGNORECASE)
             or re.search(r"\bask\s*:?\s*[\"'“‘]*\s*(?P<question>" + question_pattern + ")", text, re.IGNORECASE)
             or re.search(r"[\"'“‘]\s*(?P<question>" + question_pattern + ")", text, re.IGNORECASE)
             or re.search(r":\s*(?P<question>" + question_pattern + ")", text, re.IGNORECASE)
             or re.search(r"(?P<question>" + question_pattern + ")", text, re.IGNORECASE))
    offset = start.start("question") if start else len(text) - len(text.lstrip())
    return text[:offset], text[offset:].strip(" \t\r\n\"'“”‘’")


def interview_script(customer: str, problem: str, assumption: str = "", action: str = "") -> str | None:
    from app.idea.agent import founder_audience
    customer = founder_audience(customer) if customer else ""
    if not customer or not problem:
        return None
    from app.gtm.journey import _keywords
    # shortcut: English keywords ground the method once; use structured topics for ambiguous or multilingual interviews.
    problem = re.split(r"(?<=[.!?])\s+|[,;\n]", problem, maxsplit=1)[0].strip()
    scope = f'For {customer}, ask about "{problem}"'
    if assumption:
        scope += f', to check "{assumption.strip(" .?!")}"'
    questions = [_question_parts(question)[1] for question in re.findall(r"[^.!?\n;]*\?", action)
        if _COMMITMENT_QUESTION.match(_question_parts(question)[1])
        or not _LEADING.search(question) and not _HYPOTHETICAL.search(question)]
    topics = set(re.findall(r"[^\W_]+", _keywords(customer + " " + problem, limit=200).casefold()))
    if not topics.intersection(re.findall(r"[^\W_]+", action.casefold())):
        questions = []
    return founder_copy(scope + ".\n" + "\n".join(questions or _INTERVIEW_FALLBACK))


def ground_interview_action(action: str, script: str) -> str:
    questions = [_question_parts(line)[1] for line in script.splitlines() if line.rstrip().endswith("?")]
    if not questions:
        return action
    if "?" in action and all(_question_parts(question)[1] in questions
            for question in re.findall(r"[^.!?\n;]*\?", action)):
        return action
    used = {_question_parts(question)[1] for question in re.findall(r"[^.!?\n;]*\?", action)}

    def replace(match):
        original = match[0]
        prefix, question = _question_parts(original)
        if question in questions or _COMMITMENT_QUESTION.match(question):
            return original
        question = next((line for line in (*questions, *_INTERVIEW_FALLBACK) if line not in used),
                        "Tell me about another recent incident.")
        used.add(question)
        return prefix + question

    return re.sub(r"[^.!?\n;]*\?|(?:^|(?<=[.!\n;:]))\s*(?:what|how|when|where|why|which|who|"
                  r"tell me|describe(?=\s+(?:your\s+)?(?:favourite|favorite|opinion)\b)|walk me through)\b"
                  r"[^.!?\n;]*(?:[.!]|(?=[;\n]|$))", replace, action, flags=re.IGNORECASE)


def classify_interview(text: str) -> dict | None:
    if not _INTERVIEW.search(text):
        return None
    evidence = {"compliments": [], "facts": [], "commitments": [],
                "leading_questions": [], "hypothetical_questions": [], "weight": 0}
    # shortcut: conservative English clauses only, upgrade with grounded extraction when missed phrasing is observed.
    clauses = re.split(r"[;!?]\s*|(?<!\d)\.(?!\d)\s*|\n|\s+and\s+(?=they\b)", text[:1500], flags=re.I)
    for raw in clauses:
        clause = raw.strip()
        if not clause:
            continue
        question = bool(re.search(r"\b(?:asked|asking|question|would you|could you|do you|did you|don't you)\b", clause, re.I))
        if _LEADING.search(clause):
            evidence["leading_questions"].append(clause)
        resource = next((name for name, pattern in _RESOURCES
                         if (resource_match := pattern.search(clause)) and not _NEGATED.search(clause)), None)
        if _HYPOTHETICAL.search(clause) and not (resource and not question
                and re.search(r"\b(?:said|told|reported|agreed|committed|confirmed)\b", clause, re.IGNORECASE)
                and not re.search(r"\b(?:if|could|might|maybe|someday)\b", clause[:resource_match.start()], re.IGNORECASE)):
            if question:
                evidence["hypothetical_questions"].append(clause)
            continue
        founder_action = (re.match(r"(?:I|we)\b", clause, re.I)
                          and not re.search(r"\b(they|he|she|said|told|reported|explained)\b", clause, re.I))
        if question or founder_action:
            continue
        if resource:
            evidence["commitments"].append({"text": clause, "resource": resource})
        elif _FACT.search(clause):
            evidence["facts"].append(clause)
        elif _PRAISE.search(clause):
            evidence["compliments"].append(clause)
    if _UNHELD.search(text) and not any(evidence[key] for key in ("compliments", "facts", "commitments")):
        return None
    evidence["weight"] = 2 if evidence["commitments"] else 1 if evidence["facts"] else 0
    return evidence


def coaching_note(evidence: dict) -> str:
    note = ("You heard a concrete step involving time, an introduction or payment. Check it happens before you rely on it."
            if evidence["weight"] == 2 else
            "You heard what someone has already done. Use that detail, not praise, to choose your next test."
            if evidence["weight"] == 1 else
            "You have not described what someone has done or committed to yet. Kind words alone do not show demand.")
    if evidence["leading_questions"]:
        note += " Your question nudged someone toward saying yes."
    if evidence["hypothetical_questions"]:
        note += " Your question asked someone to guess."
    return note + ' Next time, ask: "What happened the last time you dealt with this problem?"'


def save_evidence(experiment_id: str, venture_id: str, evidence: dict) -> None:
    from app.db.supymem_models import Experiment
    from app.state import db

    # shortcut: metadata follows the confirmed result write, join its transaction when the ledger accepts metadata.
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        if row is None or row.venture_id != venture_id or row.kind != ledger.KIND:
            raise KeyError("experiment")
        context = json.loads(row.context or "{}")
        if context.get("runner") == "system" or not context.get("ran") or row.state != "concluded":
            raise ValueError("interview_result_not_recorded")
        context["result"] = {**(context.get("result") or {}), "interview_evidence": evidence,
                             "evidence_weight": evidence["weight"]}
        row.context = json.dumps(context)
        session.commit()
    ledger._invalidate(venture_id)


def blocked_reason(text: str) -> str | None:
    return next((name for name, pattern in _BLOCKERS if pattern.search(text)), None)


def blocked_alternative(venture_id: str, reason: str, hypothesis: str, blocked_id: str | None) -> dict:
    from app.chat.experiments import manual_action
    from app.core.local_mode import is_local
    from app.credits import accounts
    from app.experiments import autonomous, engine
    from app.state import db
    from core.clock import utcnow

    candidates = autonomous.available(venture_id)
    history = ledger.evidence_for_venture(venture_id)
    with db()() as session:
        direction = ledger.current_direction(session, venture_id)
    local = is_local()
    account = None if local else accounts.get_account(venture_id)
    available = accounts.balance(account)["available_micro"] if account and account.status == "active" else 0
    scans = [item for item in candidates if item.get("runner") == "scan" and item.get("state") == "proposed"
             and item.get("id") != blocked_id and item.get("venture_id") == venture_id
             and item.get("direction") == direction and not item.get("error")
             and (reason != "budget" or item.get("ceiling_micro", 0) == 0)
             and item.get("ceiling_micro", 0) >= 0
             and (local or item.get("ceiling_micro", 0) <= available)
             and not engine.blocked_reason(item, history, utcnow())]
    if scans:
        chosen = scans[0]
        next_step = "Ask me to run it when you are ready." if local else "You review the run before it starts."
        return {"body_md": founder_copy(f"You can do it another way: {chosen['title']}. I can read public sources without contacting anyone. {next_step}"),
                "action": {"label": "Try it another way", "action": "experiment.prepare",
                           "payload": {"experiment_id": chosen["id"]}, "style": "primary"}}
    alternative = {"hypothesis": "People can describe a recent attempt to address this problem: " + hypothesis,
                   "method": "Prepare a private interview script from your saved test. Do not contact anyone."}
    return {"body_md": "You can do it another way: prepare a private interview test. I can prepare your questions without a connection or spending anything. Nothing is sent or published; any contact needs your separate approval.",
            "action": manual_action(alternative)}

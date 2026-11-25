"""Product-understanding dilemmas: what the founder actually means.

Not GStack. These questions exist to resolve ambiguity in the idea before
office hours or research. Options are written from the idea text, not a
generic quiz bank.
"""

from __future__ import annotations

import re
from textwrap import shorten
from typing import Any

from core.ids import new_id
from core.copy import no_dashes


def _words(idea: str) -> str:
    return " ".join((idea or "").split())


def _lower(idea: str) -> str:
    return _words(idea).lower()


def _has(idea: str, *needles: str) -> bool:
    blob = _lower(idea)
    return any(n in blob for n in needles)


def _noun_phrase(idea: str) -> str:
    text = _words(idea)
    if not text:
        return "this product"
    text = re.sub(r"^(i need |i want |build |a |an |the )+", "", text, flags=re.I)
    return shorten(text.rstrip(" ."), width=80, placeholder="...") or "this product"


def detail_choices(options: list[str], tagged: list[str] = ()) -> list[str]:
    # shortcut: English detail cues, use model-provided tags for other wording and languages.
    cue = re.compile(r"\b(?:i|we|you)\b.{0,35}\b(?:can (?:already )?(?:describe|name|text|explain)|"
                     r"have (?:an? |a specific |a concrete )?(?:example|story)|know (?:who|the people|the person|a person|someone))\b|"
                     r"\b(?:i|we|you)\b.{0,35}\b(?:have (?:seen|observed)|"
                     r"know (?:an? |the |some )?(?:\w+ ){0,3}(?:tools?|habits?|products?|workarounds?))\b|"
                     r"\b(?:some evidence to describe|a few (?:people|customers)|"
                     r"a (?:named|specific|concrete) (?:workaround|example|moment|person))\b|"
                     r"^named (?:products|tools|alternatives|competitors)\b", re.I)
    return [option for option in options if option in tagged or cue.search(option)]


def _founder_request(value: str) -> bool:
    from app.ventures.startup_review import problem_is_request
    # shortcut: English request clauses only, use structured intent for other languages.
    return any(problem_is_request(clause) and re.match(
        r"^\s*(?:please\b|help\s+(?:me|us)\b|(?:find|show|get|give|send|tell)\s+(?:me|us)\b)", clause, re.I) or re.match(
        r"^\s*(?:(?:what|which|how|where|who|why|when)\b[^.!?;]{0,60}\b"
        r"(?:should|can|could|would|do|does|did|will|is|are)\s+(?:i|we|you)\b|"
        r"(?:can|could|would|will|may|might|do|does|did|have|has|should)\s+you\b)", clause, re.I)
        for clause in re.split(r"(?<=[.!?])\s+|[:;\n]", value))


def _template_problem(value: Any, audience: str = "") -> str:
    from app.secrets.detect import looks_like_secret
    from app.ventures.founder_facts import _workaround_statement, search_problem
    from app.ventures.startup_review import _known
    text = no_dashes(_words(str(value or "")))
    goal = re.search(r"\b(?:needs?|wants?|get|gain|find|grow|increase|attract|acquire)\b[^.!?;]{0,40}"
                     r"\b(?:customers?|users?|sales|revenue|traffic|signups?)\b", text, re.I)
    customer_goal = audience and re.match(rf"^(?:{re.escape(audience)}|they)\s+", text, re.I)
    if looks_like_secret(str(value or "")) or not _known(text) or not search_problem(text) or _founder_request(text) or goal and not customer_goal:
        return ""
    subject = rf"(?:{re.escape(audience)}|they|he|she|it|i|we|you)" if audience else r"(?:they|he|she|it|i|we|you)"
    pain = re.search(rf"(?:^|[.!?;]\s*|\band\s+)(?:{subject}\s+)?"
                     r"((?:needs?\s+help\s+with|faces?|experiences?|struggles?\s+with|suffers?\s+from|"
                     r"loses?|lost|miss(?:es|ed)?|cannot|can't|lacks?|spend(?:s|ing)?|juggles?)\s+[^.!?;]+)", text, re.I)
    if pain:
        text = pain[1]
        consequence = re.search(r"\band\s+((?:loses?|lost|miss(?:es|ed)?|cannot|can't|lacks?)\s+.+)", text, re.I)
        if consequence:
            text = consequence[1]
        return text
    # ponytail: English sentence cues, use structured short problem phrases for multilingual copy.
    sentence = re.match(r"^(?:i|we|you|they|he|she|it|my|our|their|most|many|some|all|few|several|everyone|everybody|someone)\b", text, re.I)
    clauses = len(text.split()) > 24 and re.search(r"[,;.!?]|\b(?:and|then|when|because|while|but|so)\b", text, re.I)
    return "" if sentence or clauses or _workaround_statement(text) else text


def venture_context(snapshot: dict, *, clip_audience: bool = False) -> tuple[str, str]:
    from datetime import datetime
    from app.idea.agent import ASSUMED, answer_prefill, founder_audience
    from app.secrets.detect import looks_like_secret
    from app.intake.smart import _idea_candidate, _prefill, no_idea_intent
    snapshot = {**(snapshot.get("understanding") or {}), **snapshot}
    snapshot["answered_questions"] = [pair for pair in snapshot.get("answered_questions", []) if not pair.get("reused")]
    facts = snapshot.get("founder_facts") or {}
    cleared = {field for field, saved_at in snapshot.get("founder_fact_updated_at", {}).items()
        if saved_at and not facts.get(field, {}).get("value")}
    answers = {**snapshot.get("_answered_fields", {}), **snapshot.get("answered_fields", {})}
    jobs = []
    for pair in snapshot.get("answered_questions", []):
        value = pair.get("founder_text") or pair.get("suggested_text")
        if not value:
            continue
        parsed = _prefill(value)
        scoped = answer_prefill(value, pair["field"])
        correction = scoped.get("customer")
        if (not correction and pair["field"] in {"offer", "idea", "summary"}
                and not any((answers.get("audience"), answers.get("customer"), snapshot.get("customer"),
                    snapshot.get("audience"), facts.get("audience", {}).get("value")))):
            correction = parsed.get("customer")
        if pair["field"] == "problem" and "problem" in scoped:
            parsed["problem"] = value = scoped["problem"]
        if pair.get("phase") != "gstack" and pair.get("founder_text"):
            # An answer about pilots or requirements doesn't change who the product is for.
            answers.update({key: value for key, value in parsed.items()
                            if key != "customer"})
            if correction:
                answers["customer"] = answers["audience"] = correction
        if not (pair.get("phase") == "gstack" and pair["field"] == "audience"):
            answers[pair["field"]] = (correction if pair["field"] in {"audience", "customer"} else None) or parsed.get("customer" if pair["field"] == "audience" else pair["field"]) or value
        if pair["field"] in {"problem", "workflow", "desired_outcome", "priorities"} and pair["field"] not in cleared:
            jobs.insert(0, parsed.get(pair["field"]) or value)
    for field in ("audience", "problem"):
        fact = facts.get(field, {})
        if field in cleared:
            answers[field] = ""
            if field == "audience":
                answers["customer"] = ""
            continue
        saved_at = snapshot.get("founder_fact_updated_at", {}).get(field)
        relevant = [pair.get("answered_at", "") for pair in snapshot.get("answered_questions", [])
            if pair["field"] == field or _prefill(pair.get("founder_text") or "").get("customer" if field == "audience" else field)]
        relevant += [saved_at for key, saved_at in snapshot.get("answered_field_updated_at", {}).items()
            if key == field or field == "audience" and key == "customer"]
        latest_at = max(relevant, default="")
        if fact.get("source") in {"review", "founder message", "clarification", "gstack"} and saved_at:
            try:
                if not latest_at or datetime.fromisoformat(saved_at).replace(tzinfo=None) >= datetime.fromisoformat(latest_at).replace(tzinfo=None):
                    answers[field] = fact["value"]
            except (ValueError, TypeError):
                pass
    ideas = [str(snapshot.get(key) or "") for key in ("offer", "summary", "idea", "original_idea", "founder_idea")]
    from app.ventures.startup_review import _known
    audience_answered = "audience" in answers or "customer" in answers
    audience_values = ((answers.get("audience"), answers.get("customer")) if audience_answered else
        (snapshot.get("customer"), snapshot.get("audience"), facts.get("audience", {}).get("value")))
    audience = ""
    for value in audience_values:
        candidate = _words(str(value or ""))
        if not looks_like_secret(candidate) and _known(candidate) and not _founder_request(candidate) and candidate not in {"the people you want to help", "your audience"}:
            audience = founder_audience(candidate)
            if audience:
                break
    problem_fact = facts.get("problem") or {}
    job_answers = [*jobs, *(answers.get(key) for key in ("workflow", "desired_outcome", "priorities", "problem"))]
    job_times = [pair.get("answered_at", "") for pair in snapshot.get("answered_questions", [])
        if pair["field"] in {"offer", "problem", "workflow", "desired_outcome", "priorities"}]
    job_times += [saved_at for key, saved_at in snapshot.get("answered_field_updated_at", {}).items()
        if key in {"offer", "problem", "workflow", "desired_outcome", "priorities"}]
    review_time = snapshot.get("founder_fact_updated_at", {}).get("problem")
    latest_job_time = max(job_times, default="")
    if problem_fact.get("source") in {"review", "founder message", "clarification", "gstack"} and review_time:
        try:
            if not latest_job_time or datetime.fromisoformat(review_time).replace(tzinfo=None) >= datetime.fromisoformat(latest_job_time).replace(tzinfo=None):
                job_answers = [problem_fact["value"]]
        except (ValueError, TypeError):
            pass
    problem_values = [*job_answers, snapshot.get("workflow"), snapshot.get("desired_outcome"), snapshot.get("problem"),
                      problem_fact.get("value"), problem_fact.get("quote")]
    if any(job_answers):
        ideas = []
        problem_values = job_answers
    elif "offer" in answers:
        ideas = [str(answers["offer"] or "")]
        problem_values = ideas.copy()
    if "problem" in cleared:
        ideas = []
        problem_values = job_answers
    problem = ""
    for idea in ideas:
        proposal = _idea_candidate(idea) if no_idea_intent(idea) else idea
        prefill = _prefill(proposal.removeprefix(ASSUMED)) if proposal else {}
        if not audience_answered:
            audience = audience or prefill.get("customer")
        problem_values.extend(prefill.get(key) for key in ("workflow", "desired_outcome", "problem"))
    audience = "" if looks_like_secret(str(audience or "")) else no_dashes(_words(str(audience or ""))).strip(" .,:;")
    for value in problem_values:
        text = _words(str(value or ""))
        # shortcut: English first-person goals only, use structured intent for ambiguous or multilingual goals.
        if not re.match(rf"^(?:{re.escape(audience)}|they)\s+", text, re.I) and re.search(
                r"\b(?:i|we)\s+(?:want|hope|plan|intend|aim)\b|\b(?:before|until)\s+(?:i|we)\b", text, re.I):
            continue
        problem = _template_problem(value, audience)
        if problem:
            break
    if not problem and "problem" not in cleared and "offer" not in answers:
        # shortcut: English pain clauses only, use structured extraction for other languages.
        sources = [*(str(value or "") for value in problem_values), *ideas,
                   facts.get("current_workaround", {}).get("value") or "",
                   facts.get("current_workaround", {}).get("quote") or ""]
        for source in sources:
            for clause in re.split(r"(?<=[.!?])\s+|[;\n]", source):
                phrase = re.search(rf"^(?:{re.escape(audience)}|they)\s+"
                                   r"(?:needs?\s+help\s+with|faces?|experiences?|struggles?\s+with|suffers?\s+from|"
                                   r"loses?|miss(?:es)?|cannot|can't|lacks?|spend(?:s|ing)?|juggles?)\s+", clause.strip(), re.I)
                problem = _template_problem(clause, audience) if phrase else ""
                if problem:
                    break
            if problem:
                break
    problem = no_dashes(_words(str(problem)))
    problem = re.sub(rf"^{re.escape(audience)}\s+", "", problem, flags=re.I).strip(" .,:;")
    # ponytail: English sentence scaffolding, use a saved short problem phrase for other languages.
    problem = re.sub(r"^(?:they\s+)?(?:needs?\s+help\s+with|faces?|experiences?|struggles?\s+with|suffers?\s+from)\s+", "", problem, flags=re.I)
    head = re.split(r"\b(?:when|because|while|if|due to)\b", problem, maxsplit=1, flags=re.I)[0].strip(" .,:;")
    problem = head or problem
    problem = re.sub(r"^lose(?:s)?\b", "lost", problem, flags=re.I)
    problem = re.sub(r"^miss(?:es)?\b", "missed", problem, flags=re.I)
    for pattern, replacement in ((r"^(?:cannot|can't)\b", "being unable to"), (r"^lacks?\b", "a lack of"),
                                 (r"^spends?\b", "spending"), (r"^juggles?\b", "juggling")):
        problem = re.sub(pattern, replacement, problem, flags=re.I)
    problem = shorten(problem, width=85, placeholder="").rstrip(" .,:;") or problem
    problem = re.sub(r"(?:\s+(?:a|an|the|to|of|for|with|and|or|when|because|while|if|they|are|is|their|in|at|on|from|that|can|cannot|unable))+$", "", problem, flags=re.I)
    from app.core.local_mode import is_local
    if is_local():
        from app.idea.audience import local_audience
        audience = local_audience(snapshot, audience)
    return (shorten(audience, width=60, placeholder="") if clip_audience else audience) or "the people you want to help", problem


def ground_question(question: dict, snapshot: dict | None) -> dict:
    from app.core.local_mode import is_local

    snapshot = snapshot or {}
    if is_local():
        from app.idea.audience import grounded_model_question
        grounded = grounded_model_question(question, snapshot)
        if grounded is not None:
            return grounded
    cleared = {field for field, saved_at in snapshot.get("founder_fact_updated_at", {}).items()
        if saved_at and not (snapshot.get("founder_facts") or {}).get(field, {}).get("value")}
    if not (snapshot.get("answered_questions") or snapshot.get("_answered_fields") or snapshot.get("answered_fields")
            or cleared or any(fact.get("source") == "review" or snapshot.get("founder_fact_updated_at", {}).get(field)
                and fact.get("source") in {"founder message", "clarification", "gstack"}
                for field, fact in (snapshot.get("founder_facts") or {}).items())):
        return question
    audience, job = venture_context(snapshot)
    # Saved jobs may be verb phrases; retain them for validation, not grammatical interpolation.
    if question.get("known_check") and question.get("known_field") == "audience":
        value = no_dashes(_words(str(question["known_check"].get("value") or ""))).strip(" .,:;")
        if value == audience:
            return question
    templates = {
        "problem": f"What difficulty do you want to solve for {audience}?",
        "customer": "Who needs this first?",
        "offer": f"What should your product do to help {audience}?",
        "workflow": f"What task should your product help {audience if not is_local() or len(audience) <= 60 else 'your audience'} do first?",
        "desired_outcome": f"What result would you want for {audience}?",
        "first_user_path": f"What would be the first step for {audience} to get help?",
        "pricing_hypothesis": f"How would you charge for helping {audience}?",
        "requirements": f"What must your product do to help {audience}?",
        "constraints": f"What limits must you work within to help {audience}?",
        "proposed_options": f"Which direction would best help {audience}?",
        "Q1": f"What evidence do you have that your audience ({audience}) needs help with this?",
        "Q2": f"How does your audience ({audience}) handle this today? Describe the current workaround.",
        "Q3": f"When is help most urgent for {audience}?",
        "Q4": f"What is the smallest useful version to help {audience}?",
        "Q5": f"What have you observed about how your audience ({audience}) handles this?",
        "Q6": f"What future changes could make help more useful for {audience}?",
    }
    field = question.get("gstack_id") or question.get("field")
    if isinstance(field, str) and field.startswith("gstack_"):
        field = field.removeprefix("gstack_")
    field = {"audience": "customer", "pricing": "pricing_hypothesis", "current_workaround": "Q2",
             "evidence": "Q1", "priorities": "desired_outcome"}.get(field, field)
    if field not in templates:
        return question
    def tokens(text):
        return {word.rstrip("s") for word in re.findall(r"\w+", str(text).casefold())}
    neutral = tokens(" ".join(templates.values())) - tokens(audience + " " + job)
    neutral |= tokens("which who what why how when where you your they their it this that a an the and or "
        "is are would could should can do does did will of for to with in on at from by someone people "
        "first next daily version help work use using choose select pick tell describe task step result "
        "one two three specific kind group situation sure not yet actually example give already most "
        "start getting existing tools habits current workaround time money frustration cost costs "
        "difficulty evidence commitment no checked seen need needs have has been try trying smallest "
        "useful initial product service software manually recurring plan pay each usage fixed fee "
        "audience broad reach starting keep own words person work situation understand correct understood "
        "workflow test behavior measure create creates friction become triggering moment sensitive difficult build edit needed used "
        "described saved answer answers i we me us here few possibilities any fit type enough context offer choices "
        "other something else summary confirmed separate optional questions clear place")
    for question_id in ("Q1", "Q2", "Q3"):
        for option in contextual_choices(question_id, {"customer": audience, "problem": job}):
            neutral |= tokens(option.casefold().replace(audience.casefold(), "").replace(job.casefold(), ""))
    facts = snapshot.get("founder_facts", {})
    fact_fields = {"Q1": ("evidence",), "Q2": ("current_workaround",), "Q3": ("current_workaround", "evidence"),
                   "pricing_hypothesis": ("pricing",)}.get(field, ())
    scope_fields = {"offer", "customer", "audience", "problem", "workflow", "desired_outcome", "priorities"}
    scope_times = [saved_at for key, saved_at in snapshot.get("answered_field_updated_at", {}).items() if key in scope_fields]
    scope_times += [saved_at for key, saved_at in snapshot.get("founder_fact_updated_at", {}).items() if key in {"audience", "problem"}]
    scope_times += [pair.get("answered_at", "") for pair in snapshot.get("answered_questions", []) if pair["field"] in scope_fields and not pair.get("reused")]
    from datetime import datetime
    dated_scope = []
    for saved_at in scope_times:
        try:
            dated_scope.append(datetime.fromisoformat(saved_at).replace(tzinfo=None))
        except (ValueError, TypeError):
            pass
    latest_scope = max(dated_scope, default=None)
    # shortcut: only literal audience and job spans are trusted; add semantic validation if paraphrases must survive.
    def unsupported(value):
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(str(value)):
            return True
        remaining = _words(str(value)).casefold()
        for source in (audience, job):
            if source:
                remaining = re.sub(rf"(?<!\w){re.escape(source.casefold())}(?!\w)", "", remaining)
        return bool(tokens(remaining) - neutral)
    def unsupported_option(value):
        from app.secrets.detect import looks_like_secret
        from app.ventures.founder_facts import FOUNDER_SOURCES
        if looks_like_secret(str(value)):
            return True
        remaining = _words(str(value)).casefold()
        for fact_field in fact_fields:
            fact = facts.get(fact_field, {})
            if latest_scope:
                try:
                    saved_at = datetime.fromisoformat(snapshot.get("founder_fact_updated_at", {}).get(fact_field, "")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    continue
                if saved_at < latest_scope or fact.get("source") not in FOUNDER_SOURCES:
                    continue
            source = fact.get("value")
            if source:
                remaining = re.sub(rf"(?<!\w){re.escape(_words(source).casefold())}(?!\w)", "", remaining)
        return unsupported(remaining)
    from app.idea.agent import core_followup
    history = snapshot.get("answered_questions", [])
    followups = {core_followup(core_field, attempt, answer=answer)
        for core_field in ("offer", "customer") for attempt in range(2)
        for answer in ["", *(pair.get("answer", "") for pair in history if not pair.get("resolved"))]}
    fields = set(snapshot.get("_answered_fields", {})) | set(snapshot.get("answered_fields", {}))
    job_fields = {"problem", "workflow", "desired_outcome", "priorities"}
    strict_options = bool(cleared or fields & job_fields or any(pair["field"] in job_fields | {"audience"} for pair in history)
        or facts.get("problem", {}).get("source") == "review"
        or snapshot.get("founder_fact_updated_at", {}).get("problem") and facts.get("problem", {}).get("source") in {"founder message", "clarification", "gstack"})
    rewritten = question.get("text", "") not in followups and (unsupported(question.get("text", "")) or cleared
        or is_local() and field == "workflow" and len(audience) > 60 and audience.casefold() in _words(question.get("text", "")).casefold()
        or job and re.search(rf"\b(?:with|from|handle)\s+{re.escape(job)}(?!\w)", question.get("text", ""), re.I)
        or job and "options" in question and job.casefold() not in _words(question.get("text", "")).casefold()
            and question.get("text", "") != templates[field]
        or audience != "the people you want to help" and "options" in question
            and audience.casefold() not in _words(question.get("text", "")).casefold())
    question = dict(question)
    if rewritten:
        replaced = question.get("text", "") != templates[field]
        # A replaced job question asks something new; other fields' model choices still answer the same field.
        kept = [] if replaced and field in job_fields else [
            option for option in question.get("options", []) if not strict_options or not unsupported_option(option)]
        options = (contextual_choices(field, snapshot) if replaced else []) or kept
        question.update(text=templates[field], options=options or contextual_choices(field, snapshot), why="",
            requires_detail=[option for option in question.get("requires_detail", []) if option in options],
            detail_prompt="Type your own answer." if replaced else question.get("detail_prompt") or "Type your own answer.",
            recommended_option="", recommendation_reason="")
        question.pop("known_check", None)
        question.pop("known_field", None)
    elif strict_options:
        question["options"] = [option for option in question.get("options", []) if not unsupported_option(option)]
        if question.get("recommended_option") not in question["options"]:
            question.update(recommended_option="", recommendation_reason="")
        question["requires_detail"] = [option for option in question.get("requires_detail", []) if option in question["options"]]
    for key in ("detail_prompt", "why", "recommendation_reason", "prefill", "explain"):
        if (unsupported_option if key == "prefill" else unsupported)(question.get(key, "")):
            question[key] = ""
    return question


def contextual_choices(question_id: str, snapshot: dict) -> list[str]:
    audience, problem = venture_context(snapshot)
    if not problem or audience == "the people you want to help":
        return []
    if question_id == "Q1":
        return ["I have no evidence yet.",
                "I have seen a real example.",
                "I have seen someone commit to addressing it."]
    if question_id == "Q2":
        return ["I know the tools or habits they use today.",
                "I know an existing product they use for this.",
                "I have not checked how they handle this today."]
    if question_id == "Q3":
        facts = snapshot.get("founder_facts") or {}
        workaround = no_dashes(_words(facts.get("current_workaround", {}).get("value") or "")).rstrip(" .")
        evidence = no_dashes(_words(facts.get("evidence", {}).get("value") or "")).rstrip(" .")
        workaround = "" if _founder_request(workaround) else workaround
        evidence = "" if _founder_request(evidence) else evidence
        return ["I know a specific moment when they need help with this.",
                ("I expect them to seek help when they use that workaround." if workaround else
                 "I expect them to seek help when they need to prevent it."),
                ("I expect them to seek help in the situation I described." if evidence else
                 "I expect them to seek help when they need support.")]
    return []


def _opt(oid: str, label: str, implication: str, *, recommended: bool = False) -> dict[str, Any]:
    return {
        "id": oid,
        "label": label,
        "implication": implication,
        "recommended": recommended,
        "requires_detail": bool(detail_choices([label])),
        "detail_prompt": "Describe your example: who, when, what happened?",
    }


def _q(
    slot_key: str,
    prompt: str,
    why: str,
    options: list[dict[str, Any]],
    *,
    selection_mode: str = "single",
    exclusive_option_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": new_id("qst"),
        "slot_key": slot_key,
        "process": "product_understanding",
        "prompt": prompt,
        "why_asked": why,
        "allow_other": True,
        "selection_mode": selection_mode,
        "exclusive_option_ids": list(exclusive_option_ids),
        "options": options,
    }


def suggested_answers(idea: str, field: str, options: list[str] = ()) -> list[str]:
    source = no_dashes(_words(idea)).casefold()
    choices = list(dict.fromkeys(no_dashes(option.strip()) for option in options if isinstance(option, str) and option.strip()
        and not (source and source in no_dashes(_words(option)).casefold())))[:4]
    if len(choices) >= 2:
        return choices
    try:
        from app.intake.smart import _prefill
        audience = _prefill(idea).get("customer", "")
        if audience and field in {"customer", "audience", "gstack_Q3"}:
            generated = [f"{audience} who handle this themselves", f"{audience} who manage a team", f"{audience} using another tool today"]
        elif field == "offer":
            audience, _ = venture_context({"idea": idea})
            generated = [f"A self-serve tool for {audience}", f"A guided service for {audience}",
                         f"Software with hands-on help for {audience}"]
        else:
            generated = []
    except Exception:
        generated = []
    fallback = (["People handling this themselves", "Teams handling this together", "People using another tool"]
                if field in {"customer", "audience", "gstack_Q3"} else
                ["Handle it manually", "Use an existing tool", "Ask someone else to help"])
    return list(dict.fromkeys(choices + [no_dashes(option) for option in generated] + fallback))[:3]


def dilemmas_for_idea(idea: str, *, name: str = "") -> list[dict[str, Any]]:
    """Three or four dilemmas that split what the founder might mean."""
    phrase = _noun_phrase(idea)
    product = (name or "").strip() or phrase
    questions: list[dict[str, Any]] = []

    # ponytail: substring keywords; "track" alone matched "tracks flour inventory", use word matching if more collide
    if _has(idea, "song", "playlist", "music", "spotify"):
        questions.append(
            _q(
                "product_object",
                f"When you say “{phrase}”, what is the thing people open on day one?",
                "A song catalog, a playlist store, and a curation tool are three different products.",
                [
                    _opt(
                        "opt_a",
                        "A place to find and get songs, then group them into playlists I can sell",
                        "Catalog plus checkout. Curation is the wedge; a full music service waits.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "A marketplace where curators sell finished playlists to listeners",
                        "Two-sided market. First job is matching a buyer to a playlist, not ingesting songs.",
                    ),
                    _opt(
                        "opt_c",
                        "A tool that helps me assemble playlists. Selling can wait",
                        "Authoring first. Commerce is inferred later, not built first.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_buyer",
                "Who pays first, or is this free until someone would miss it?",
                "The first customer decides the copy, the wedge, and who you watch.",
                [
                    _opt(
                        "opt_a",
                        "Listeners who buy a playlist someone else made",
                        "Consumer checkout. Curators are supply, not the first invoice.",
                    ),
                    _opt(
                        "opt_b",
                        "Curators who need a place to sell their taste",
                        "B2B-ish: the curator is the customer. Listeners are the audience.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_c",
                        "I’m not sure who pays yet. I want to see people use it first",
                        "Observation first. Do not invent a pricing page until you watch one session.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_job",
                "What must work this week, or it is not the product?",
                "If everything is in scope, nothing is. Name the one job.",
                [
                    _opt(
                        "opt_a",
                        "I can group songs into a named playlist and share or sell that list",
                        "Playlist as the object. Ingest and discovery can be messy as long as the list is real.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "I can search and get the songs I want, reliably",
                        "Catalog quality is the product. Grouping waits.",
                    ),
                    _opt(
                        "opt_c",
                        "A stranger can discover my playlist and pay without talking to me",
                        "Storefront first. Your own authoring tools can stay manual.",
                    ),
                ],
            )
        )
    elif _has(idea, "marketplace", "buy", "sell", "shop"):
        questions.append(
            _q(
                "product_object",
                f"Is {product} a marketplace, a store you operate, or a tool that sits next to one?",
                "Those three look the same in a sentence and are different companies.",
                [
                    _opt(
                        "opt_a",
                        "Two sides: buyers and sellers meet here",
                        "Liquidity is the product. One side will be empty on day one. Pick which you seed.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "I sell (or fulfill) the thing myself",
                        "A store. Marketplace language comes out of the copy.",
                    ),
                    _opt(
                        "opt_c",
                        "A tool sellers or buyers use on top of something that already exists",
                        "Wedge is software, not the market itself.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_buyer",
                "Who is the first real person this is for, not a category?",
                "“Users” is not a customer. We need a real person before we can test demand.",
                [
                    _opt(
                        "opt_a",
                        "Someone I can already name or text today",
                        "Design partner path. Research watches that person, not a TAM slide.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "A tight role I know (job + context), but I do not have a name yet",
                        "Next step is five names. Do not write generic landing copy.",
                    ),
                    _opt(
                        "opt_c",
                        "Anyone in the market. I have not picked a first person",
                        "That is a flag. Next we will work out who needs this most.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_job",
                "What is the first finished job, the thing they would pay for this week?",
                "Platforms fail here. One job is a company you can watch.",
                [
                    _opt(
                        "opt_a",
                        "One transaction or booking completes end to end",
                        "The wedge is the completed job, not the catalog.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "They can list or browse; payment can wait",
                        "Demand test. Charge later, but say so.",
                    ),
                    _opt(
                        "opt_c",
                        "Several jobs at once: listing, search, chat, payouts",
                        "Too wide. Research will treat this as a platform bet and push back.",
                    ),
                ],
            )
        )
    else:
        object_options = suggested_answers(idea, "offer")
        buyer_options = suggested_answers(idea, "customer")
        questions.append(
            _q(
                "product_object",
                f"What is {product}, in one object a person can point at?",
                "If we cannot name the object, research could end up looking at a different product.",
                [
                    _opt(
                        "opt_a",
                        object_options[0],
                        "We treat the sentence as the product and research that, not a category.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        object_options[1],
                        "Concierge is allowed. The software is not the company yet.",
                    ),
                    _opt(
                        "opt_c",
                        object_options[2],
                        "We will not pretend we understood it. The next questions start after you pick an object.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_buyer",
                "Who is the first person this is for?",
                "Ambiguous audience is how we build the wrong thing.",
                [
                    _opt(
                        "opt_a",
                        buyer_options[0],
                        "We write the brief around that person.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        buyer_options[1],
                        "Brief stays tight. Research looks for that role’s current workaround.",
                    ),
                    _opt(
                        "opt_c",
                        buyer_options[2],
                        "We will say so on the brief. Do not invent a persona.",
                    ),
                ],
            )
        )
        questions.append(
            _q(
                "product_job",
                "What has to work, or this is not the product?",
                "This is the job research will look for substitutes of.",
                [
                    _opt(
                        "opt_a",
                        "One painful job they already try to do another way",
                        "We research the workaround, not the vision.",
                        recommended=True,
                    ),
                    _opt(
                        "opt_b",
                        "A new habit they do not have yet",
                        "Demand is unproven. Research will look for adjacent behavior, not a category TAM.",
                    ),
                    _opt(
                        "opt_c",
                        "Several jobs. The product is a suite",
                        "We will still pick one job for the brief. The rest waits.",
                    ),
                ],
            )
        )

    questions.append(
        _q(
            "product_today",
            "What do they do today instead of this?",
            "If we do not know the substitute, we do not know the product.",
            [
                _opt(
                    "opt_a",
                    "A named workaround: spreadsheet, inbox, agency, or a specific app",
                    "Research hunts that substitute and the people complaining about it.",
                    recommended=True,
                ),
                _opt(
                    "opt_b",
                    "They already pay a product I can name",
                    "We research that product’s gaps, not “the market.”",
                ),
                _opt(
                    "opt_c",
                    "I do not know what they do today",
                    "Honest. Research’s first job is to find the substitute, not to score demand.",
                ),
            ],
            selection_mode="multiple",
            exclusive_option_ids=("opt_c",),
        )
    )
    questions.append(
        _q(
            "product_expertise",
            "How well do you know this market yourself?",
            "Your expertise changes what I research and what I take as already true.",
            [
                _opt(
                    "opt_a",
                    "I work in this world. I know the people and the usual workarounds.",
                    "I will treat you as the expert and use research to check substitutes, not to lecture you.",
                    recommended=True,
                ),
                _opt(
                    "opt_b",
                    "I know the problem as a customer, not as an insider.",
                    "I will research the competitive map and name who already serves this person.",
                ),
                _opt(
                    "opt_c",
                    "I am new here. Map the market for me.",
                    "Research’s first job is to teach both of us who already wins and where they fail.",
                ),
            ],
        )
    )
    questions.append(
        _q(
            "product_market",
            "What should I treat as already true about this market?",
            "I should not re-ask what you already know, and I should not invent a market you did not claim.",
            [
                _opt(
                    "opt_a",
                    "I can name who wins today and why people still complain.",
                    "Write those names in the brief. Research checks them instead of starting from zero.",
                    recommended=True,
                ),
                _opt(
                    "opt_b",
                    "I know the customer. I do not know the competitive map yet.",
                    "Research hunts substitutes for that person, not a generic category slide.",
                ),
                _opt(
                    "opt_c",
                    "Treat my idea as a hypothesis. Prove or kill it with research.",
                    "Honest. The brief stays tight and research is allowed to disagree.",
                ),
            ],
        )
    )
    return questions


def is_product_questions(questions: list[dict[str, Any]] | None) -> bool:
    """True when this round is product understanding, not GStack office hours."""
    rows = [q for q in (questions or []) if isinstance(q, dict)]
    if not rows:
        return False
    return all(
        q.get("process") == "product_understanding"
        or str(q.get("slot_key") or "").startswith("product_")
        for q in rows
    )


def _label(answers: dict[str, Any], questions: list[dict[str, Any]], slot: str) -> str:
    q = next((row for row in questions if row.get("slot_key") == slot), None)
    raw = answers.get(slot) or (answers.get(q["id"]) if q else "") or ""
    raw = str(raw).strip()
    for opt in (q or {}).get("options") or []:
        if raw in {opt.get("id"), opt.get("label")}:
            if opt.get("requires_detail"):
                return ((answers.get("_answer_provenance") or {}).get(slot) or {}).get("founder_text") or ""
            raw = str(opt.get("label") or raw)
            break
    if ((answers.get("_answer_provenance") or {}).get(slot) or {}).get("provenance") == "suggested_choice":
        return "Untested assumption: " + raw
    return raw


def brief_from_answers(
    *,
    idea: str,
    name: str = "",
    answers: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    answers = answers or {}
    questions = questions or []
    phrase = _noun_phrase(idea)
    product = (name or "").strip() or phrase
    obj = _label(answers, questions, "product_object")
    buyer = _label(answers, questions, "product_buyer")
    job = _label(answers, questions, "product_job")
    today = _label(answers, questions, "product_today")
    expertise = _label(answers, questions, "product_expertise")
    market = _label(answers, questions, "product_market")
    one_liner = obj or phrase
    what_not = "A generic platform, a second user type, or anything that is not the first job."
    if _has(idea, "song", "playlist", "music"):
        if "tool" in obj.lower() or "wait" in obj.lower():
            what_not = "A full music streaming service or a two-sided marketplace on day one."
        elif "marketplace" in obj.lower() or "curator" in obj.lower():
            what_not = "A general song store or a Spotify clone."
        else:
            what_not = "A streaming service. The object is the playlist someone can sell or share."
    understood = (
        f"I heard {product} as {one_liner}. First person: {buyer or 'not named yet'}. "
        f"Must work first: {job or 'not named yet'}. Today they use: {today or 'unknown'}. "
        f"Your edge: {expertise or 'not named yet'}."
    )
    return {
        "process": "product_understanding",
        "name": product,
        "idea": _words(idea),
        "one_liner": one_liner,
        "understood": understood,
        "what_it_is": obj or f"The founder described: {phrase}.",
        "what_it_is_not": what_not,
        "first_user": buyer or "Not named yet.",
        "first_job": job or "Not named yet.",
        "today": today or "Substitute unknown.",
        "expertise": expertise or "Not named yet.",
        "market": market or "Not named yet.",
        "research_query": " · ".join(p for p in (one_liner, job, today, expertise) if p),
    }

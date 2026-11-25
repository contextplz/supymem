"""One Flash copy call for the first look (45 §3.3).

Plain drafts are free in explicit offline mode. Paid unknown outcomes pause;
only confirmed, unusable model output can become a plain fallback.
"""

from __future__ import annotations

from functools import partial
import hashlib
import json
import logging
import os
import re
from typing import Any

from llm_gateway import sole

from app.firstlook.lint import lint_copy
from app.gstack.prompts import design_copy_rules

PURPOSE = "firstlook.copy"
TIER = "flash"
COPY_MODE_ENV = "FIRSTLOOK_COPY_MODE"

_FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.M)

_SYSTEM = (
    "You write honest first-look landing page copy from one idea sentence. "
    "Make it specific to THIS idea, never generic SaaS filler. "
    + design_copy_rules()
    + " "
    "Return ONLY strict JSON with keys: headline (≤90 chars, what it is, no "
    "wordplay), subhead (≤140 chars, who + outcome), points (exactly 3 objects "
    "with title ≤36 and body ≤180), how_it_works (optional array of 3 "
    "{step,text≤80}), cta ({label≤22, kind: waitlist|notify|book|subscribe}), "
    "audience (short phrase), name_candidates (exactly 3 short names), "
    "assumptions (1-3 plain assumptions you made). "
    "This is a CONCEPT page for an idea that is not built yet. Write like a founder explaining "
    "what they are building and why: use 'We're building…', 'The idea is…', 'We want to…'. NEVER "
    "state in the present tense that the product does something ('it tracks', 'we send', 'you can "
    "book', 'helps you save') and never describe features, screens or results as if they exist. "
    "If founder_name is provided, say it is by that founder (first person is fine); never invent a "
    "name. Exactly one call to action: join the waitlist (cta kind waitlist, label 'Join the waitlist'). "
    "If competitors are provided, mention differentiation without inventing facts. "
    "Never invent users, revenue, markets, testimonials, partners, prices, "
    "dates, or guarantees. No 'trusted by', 'AI-powered', 'seamless', "
    "'best', or '#1'. "
    "If founder_instruction is present, follow it for wording and emphasis, "
    "but every rule above still wins over it."
)


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text_of(response: Any) -> str:
    choices = _attr(response, "choices") or []
    if not choices:
        return ""
    message = _attr(choices[0], "message")
    return str(_attr(message, "content") or "")


def _parse_json(text: str) -> dict[str, Any] | None:
    cleaned = _FENCE_RE.sub("", text or "").strip()
    if not cleaned:
        return None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _force_fallback() -> bool:
    from app.core.local_mode import is_local, is_offline
    if is_local():
        return is_offline()
    mode = (os.environ.get(COPY_MODE_ENV) or "").strip().lower()
    if mode in {"fallback", "plain", "offline", "mock"}:
        return True
    if mode in {"live", "model"}:
        return False
    # Unset means live, like chat/assistant.py. Defaulting to offline made prod
    # (which never sets LLM_OFFLINE) silently serve the plain template.
    return os.environ.get("LLM_OFFLINE", "false").lower() in {"1", "true", "yes"}


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    head = text[: n - 1].rsplit(" ", 1)[0] or text[: n - 1]
    return f"{head.rstrip(' ,;:-')}…"


def _competitor_label(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or item.get("website") or "").strip()
    return str(item or "").strip()


_ASK_RE = re.compile(r"^(?:(?:hi|hey|hello)\b[,!. ]*)?(?:i|we)(?:['’]d|['’]m| would| want| wanna| need| plan| hope| am trying| am going)?"
                     r"(?: like| trying| going)?(?: to)?\s+", re.I)
_PRODUCT_RE = re.compile(
    r"^(?:start|build|create|launch|make|found|run)\s+(?:a|an|the|my)\s+"
    r"(?:\w+\s+){0,2}?(?:company|startup|business|product|app|tool|platform|service)\s+(that|which|to)\s+",
    re.I,
)


def third_person(text: str, subject: str = "This product") -> str:
    """Rewrite a first-person ask ("I want to start a company that helps X") into product copy."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not re.match(r"^(?:(?:hi|hey|hello)\b[,!. ]*)?(?:i|we)\b", text, re.I):
        return text
    rest = _ASK_RE.sub("", text, count=1)
    hit = _PRODUCT_RE.match(rest)
    if hit:
        body = rest[hit.end():]
        rest = f"{subject} {body}" if hit.group(1).lower() != "to" else f"{subject} exists to {body}"
    return rest[:1].upper() + rest[1:]


def fallback_copy(
    idea_text: str,
    *,
    audience: str | None = None,
    research: dict[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Idea-shaped page without a model — still specific, never one bland template."""
    sentence = third_person(idea_text, (name or "").strip() or "This product")
    if not sentence:
        sentence = "A new venture"
    first = re.split(r"[.!?]", sentence, 1)[0].strip()
    words = first.split()
    headline_src = " ".join(words[:12]) if len(words) > 14 else first
    headline = _clip(headline_src.rstrip(".,;:"), HEADLINE_BUDGET)
    who = (audience or "people who need this").strip() or "people who need this"
    if who.lower() in {"consumers", "users", "people"}:
        who = "people who need this"
    words = [w for w in re.findall(r"[A-Za-z0-9]+", sentence) if len(w) > 2][:4]
    stem = "".join(w.title() for w in words[:2])[:18] or "Venture"
    competitors = [
        label
        for label in (_competitor_label(c) for c in (research or {}).get("competitors") or [])
        if label
    ][:3]
    demand = str((research or {}).get("demand") or "")
    point3_body = "This is a first look. Nothing here is final."
    if competitors:
        point3_body = _clip(
            f"Names already in this space: {', '.join(competitors)}. We'll differentiate as the plan locks.",
            160,
        )
    elif demand and demand != "unknown":
        point3_body = _clip(f"Early demand read: {demand}. We'll refine once you answer a few questions.", 160)
    return {
        "headline": headline,
        "subhead": _clip(f"For {who}, a first look, not a finished product.", 140),
        "points": [
            {
                "title": "The job to be done",
                "body": _clip(sentence, 180),
            },
            {
                "title": "Who this is for",
                "body": _clip(f"{who}, starting with the smallest useful slice.", 160),
            },
            {
                "title": "Market notes",
                "body": point3_body,
            },
        ],
        "how_it_works": [
            {"step": 1, "text": _clip(f"Start with: {sentence}", 80)},
            {"step": 2, "text": "Capture interest from the right first users."},
            {"step": 3, "text": "Lock scope, then expand only what earns its keep."},
        ],
        "cta": {"label": "Join the waitlist", "kind": "waitlist"},
        "audience": who,
        "name_candidates": [
            stem[:20],
            f"{stem} Lab"[:20],
            f"{(words[0].title() if words else 'New')} Co"[:20],
        ],
        "assumptions": [
            "I used your words as the headline.",
            f"I assumed the audience is {who}.",
            *( [f"Competitors mentioned in research: {', '.join(competitors)}."] if competitors else [] ),
        ],
        "fallback": True,
    }


# Keep in sync with lint.HEADLINE_MAX without importing circularly at module load.
HEADLINE_BUDGET = 90


def copy_hash(copy: dict[str, Any]) -> str:
    # Exclude runtime flags so "try another look" keeps the same copy hash.
    material = {k: v for k, v in copy.items() if k not in {"fallback", "_render_snapshot"}}
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _normalize(raw: dict[str, Any], idea_text: str) -> dict[str, Any]:
    points_in = raw.get("points") or []
    points: list[dict[str, str]] = []
    for item in points_in[:3]:
        if isinstance(item, dict):
            points.append(
                {
                    "title": _clip(str(item.get("title") or "Point"), 36),
                    "body": _clip(str(item.get("body") or ""), 180),
                }
            )
        else:
            points.append({"title": "Point", "body": _clip(str(item), 110)})
    while len(points) < 3:
        points.append(
            {
                "title": "More soon",
                "body": "Details will firm up once the plan is locked.",
            }
        )

    cta_raw = raw.get("cta")
    if isinstance(cta_raw, dict):
        cta = {
            "label": _clip(str(cta_raw.get("label") or "I'm interested"), 22),
            "kind": str(cta_raw.get("kind") or "waitlist"),
        }
    elif isinstance(cta_raw, str) and cta_raw.strip():
        cta = {"label": _clip(cta_raw, 22), "kind": "waitlist"}
    else:
        cta = {"label": "I'm interested", "kind": "waitlist"}

    how = []
    for i, step in enumerate(raw.get("how_it_works") or [], start=1):
        if isinstance(step, dict):
            text = _clip(str(step.get("text") or ""), 80)
            if text:
                how.append({"step": i, "text": text})
        else:
            text = _clip(str(step), 80)
            if text:
                how.append({"step": i, "text": text})

    assumptions = [str(a).strip() for a in (raw.get("assumptions") or []) if str(a).strip()]
    if not assumptions:
        assumptions = ["I filled gaps from your one sentence."]

    names = [str(n).strip()[:28] for n in (raw.get("name_candidates") or []) if str(n).strip()]
    while len(names) < 3:
        names.append(f"Name {len(names) + 1}")

    return {
        "headline": _clip(str(raw.get("headline") or idea_text), HEADLINE_BUDGET),
        "subhead": _clip(str(raw.get("subhead") or "A first look at the idea."), 140),
        "points": points[:3],
        "how_it_works": how[:3] or None,
        "cta": cta,
        "audience": _clip(str(raw.get("audience") or "people who need this"), 80),
        "name_candidates": names[:3],
        "assumptions": assumptions[:3],
        "fallback": bool(raw.get("fallback")),
    }


def quiz_option_labels() -> list[str]:
    from app.gstack.methodology import FORCING_QUESTIONS

    labels: list[str] = []
    for spec in FORCING_QUESTIONS.values():
        for option in spec.get("options") or []:
            label = str(option.get("label") or "").strip()
            if len(label) >= 12:
                labels.append(label)
    return labels


def strip_quiz_leaks(copy: dict[str, Any], *, idea_text: str = "") -> dict[str, Any]:
    """Remove clarify-quiz option labels that leaked onto the landing page."""
    labels = quiz_option_labels()
    patched = dict(copy)

    def _clean(value: str, fallback: str = "") -> str:
        text = str(value or "").strip()
        for label in labels:
            if text == label or label in text:
                return fallback
        return text

    idea = (idea_text or "").strip()
    patched["headline"] = _clean(patched.get("headline") or "", idea[:HEADLINE_BUDGET] or "A first look")
    patched["subhead"] = _clean(
        patched.get("subhead") or "",
        "A first look, waitlist only until the offer is real.",
    )
    points = []
    for point in patched.get("points") or []:
        if not isinstance(point, dict):
            continue
        title = _clean(str(point.get("title") or ""), "What this is")
        body = _clean(str(point.get("body") or ""), idea[:160] or "Details will firm up from your brief.")
        if title or body:
            points.append({"title": title or "What this is", "body": body})
    while len(points) < 3:
        points.append({"title": "More soon", "body": "Details will firm up once the plan is locked."})
    patched["points"] = points[:3]
    steps = []
    for step in patched.get("how_it_works") or []:
        if isinstance(step, dict):
            text = _clean(str(step.get("text") or ""))
            if text:
                steps.append({"step": step.get("step") or len(steps) + 1, "text": text})
        else:
            text = _clean(str(step))
            if text:
                steps.append({"step": len(steps) + 1, "text": text})
    patched["how_it_works"] = steps[:3] or None
    assumptions = [
        item
        for item in (patched.get("assumptions") or [])
        if str(item).strip() and not any(label in str(item) for label in labels)
    ]
    patched["assumptions"] = assumptions[:3] or ["I used the words you just gave me."]
    return patched


async def _call_model_legacy(
    idea_text: str,
    *,
    audience: str | None = None,
    research: dict[str, Any] | None = None,
    instruction: str | None = None,
    founder_name: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
) -> tuple[dict[str, Any] | None, float]:
    import asyncio
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import load_provider_config_from_env
    from llm_gateway.usage import extract_confirmed_token_usage

    from app.chat.assistant import _rejection_release_confirmed
    from app.credits.accounts import account_for_workspace, get_account
    from app.credits.errors import CreditError, CreditsUnavailable
    from app.credits.estimate import actual_llm_micro, count_prompt_tokens, estimate_llm_micro
    from app.credits.gate import charge_sync
    from app.credits.units import MICRO_PER_USD
    from app.db.models import CreditReservation
    from app.llm import founder_provider
    from app.llm.provider_errors import definitive_provider_rejection
    from app.state import db

    user = json.dumps(
        {
            "idea": idea_text,
            "audience_hint": audience or "",
            "competitors": (research or {}).get("competitors") or [],
            "demand": (research or {}).get("demand") or "",
            **({"founder_name": founder_name[:80]} if founder_name else {}),
            **({"founder_instruction": instruction[:600]} if instruction else {}),
        },
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    wid = (workspace_id or sole.get_active_workspace_id() or "").strip()
    if not wid:
        raise CreditsUnavailable("firstlook_workspace_required")
    founder_provider.register_workspace_llm_resolver()
    config = founder_provider.resolve_llm_config(wid)
    byok = config.source == "founder_override"
    if byok:
        captured = founder_provider.to_provider_config(wid, resolved_config=config)
        if captured is None:
            raise CreditsUnavailable("firstlook_provider_config_unavailable")
    else:
        platform = load_provider_config_from_env()
        # The pinned single-attempt door selects pro, but the actual model is
        # this captured flash deployment. No workspace re-resolution at dispatch.
        captured = platform.model_copy(deep=True, update={
            "deployment_pro": platform.deployment_flash, "source": "platform_default"})
    from llm_gateway.routing import canonicalize_model_config, sol_output_cap
    route = canonicalize_model_config(captured.model_copy(deep=True), captured.deployment_pro)
    max_tokens = sol_output_cap(route, 900)
    if route.api_mode == "responses":
        captured = route
    model = captured.deployment_pro
    account = get_account(venture_id) if venture_id else account_for_workspace(wid)
    if account is not None:
        from app.credits.accounts import account_can_spend
        with db()() as session:
            allowed = account_can_spend(session, account, wid, venture_id) if venture_id else (account.scope == "person" or (account.scope == "venture" and account.workspace_id == wid))
        if not allowed:
            raise CreditsUnavailable("firstlook_credit_scope_mismatch")
    if not byok and account is None:
        raise CreditsUnavailable("firstlook_credit_account_required")
    if account is not None:
        from sqlalchemy import select

        with db()() as session:
            pending = session.scalar(select(CreditReservation.id).where(
                CreditReservation.account_id == account.id, CreditReservation.workspace_id == wid,
                CreditReservation.purpose == PURPOSE, CreditReservation.status == "reconciling"))
        if pending is not None:
            raise ProviderCallError("provider_outcome_unknown", "The saved provider outcome is unconfirmed.")
    estimate = estimate_llm_micro(model, prompt_tokens=count_prompt_tokens(messages), max_tokens=max_tokens)
    failure = None
    provider_attempted = False
    try:
        with charge_sync(workspace_id=wid, venture_id=venture_id, account_id=account.id if account else None,
                         purpose=PURPOSE, estimate_micro=estimate, model=model, provider=captured.provider,
                         payer="founder" if byok else None) as meter:
            if not byok and (meter.passthrough or meter.hold is None):
                raise CreditsUnavailable("firstlook_credit_reservation_required")
            meter.protect_outcome()
            try:
                # One dispatch only. A timeout cannot authorize another provider or draft.
                with sole.workspace_llm_context(wid):
                    provider_attempted = True
                    response = await asyncio.wait_for(sole.transmit(
                        messages, purpose=PURPOSE, tier="pro", max_tokens=max_tokens,
                        workspace_id=wid, config=captured, use_workspace_override=False,
                        single_attempt=True), timeout=30.0)
            except ProviderCallError as exc:
                failure = exc
                if definitive_provider_rejection(exc):
                    meter.confirm_unspent()
            except Exception as exc:
                raise ProviderCallError("provider_outcome_unknown", "The provider outcome is unconfirmed.") from exc
            if failure is None:
                try:
                    counts = extract_confirmed_token_usage(_attr(response, "usage"))
                    if counts is None:
                        raise ProviderCallError("provider_outcome_unknown", "Provider usage is unconfirmed.")
                    cost = actual_llm_micro(model, prompt_tokens=counts["prompt_tokens"],
                                            completion_tokens=counts["completion_tokens"]) / MICRO_PER_USD
                    meter.record(prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"])
                except ProviderCallError:
                    raise
                except Exception as exc:
                    raise ProviderCallError("provider_outcome_unknown", "Provider usage is unconfirmed.") from exc
    except CreditError as exc:
        if not provider_attempted:
            raise
        raise ProviderCallError("provider_outcome_unknown", "Provider billing needs reconciliation.") from exc
    if failure is not None:
        if definitive_provider_rejection(failure):
            if not _rejection_release_confirmed(meter, workspace_id=wid, byok=byok):
                raise ProviderCallError("provider_outcome_unknown", "Provider rejection release is unconfirmed.") from failure
            failure.unspent_confirmed = True
        raise failure
    if not byok and meter.settle_result is None:
        raise ProviderCallError("provider_outcome_unknown", "Provider settlement is unconfirmed.")
    from app.pagebuilder.jobs import EDIT_RECEIPTS
    if EDIT_RECEIPTS.get() is not None and not byok:
        from app.credits.gate import captured_receipt
        usage = captured_receipt(meter)
        EDIT_RECEIPTS.get().append({**usage, "credits_micro": meter.settle_result.actual_micro, "settled": True})
    # Content/JSON rejection after a confirmed paid receipt is known unusable output.
    try:
        text = _text_of(response)
    except (TypeError, ValueError, AttributeError):
        return None, cost
    parsed = _parse_json(text)
    if parsed is None:
        logging.getLogger(__name__).warning("firstlook_copy_unparseable: len=%d", len(text))
        return None, cost
    try:
        return _normalize(parsed, idea_text), cost
    except (TypeError, ValueError, AttributeError) as exc:
        # Model JSON of an unexpected shape is a fallback, never a 500.
        logging.getLogger(__name__).warning("firstlook_copy_unparseable: %r", exc)
        return None, cost


async def _call_model(
    idea_text: str,
    *,
    audience: str | None = None,
    research: dict[str, Any] | None = None,
    instruction: str | None = None,
    founder_name: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    generation_intent: str | None = None,
    call_slot: str = "first",
) -> tuple[dict[str, Any] | None, float]:
    if os.getenv("COS_FIRSTLOOK_CAPTURE_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return await _call_model_legacy(idea_text, audience=audience, research=research,
            instruction=instruction, founder_name=founder_name, workspace_id=workspace_id, venture_id=venture_id)
    import asyncio
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import load_provider_config_from_env

    from app.credits import metered
    from app.credits.accounts import account_for_workspace, get_account
    from app.credits.errors import CreditsUnavailable
    from app.credits.units import MICRO_PER_USD
    from app.firstlook.admission import (generation_call_id, generation_identity, checkpoint_generation_output,
        read_generation_output, validate_generation_output)
    from app.intake.model_capture import capture_request
    from app.llm import founder_provider
    from app.secrets.detect import looks_like_secret
    from app.state import db

    wid = (workspace_id or sole.get_active_workspace_id() or "").strip()
    if not wid:
        raise CreditsUnavailable("firstlook_workspace_required")
    request_id = generation_identity(generation_intent, slot=call_slot)
    key = metered.model_call_key(workspace_id=wid, venture_id=venture_id,
        purpose=PURPOSE, request_id=request_id)

    def recovered_output(saved):
        if saved["outcome"] != "confirmed":
            raise CreditsUnavailable("original Firstlook request was rejected")
        exists, output = read_generation_output(generation_intent,
            workspace_id=wid, venture_id=venture_id, slot=call_slot)
        if saved["output_usable"] and not exists:
            raise CreditsUnavailable("saved Firstlook output needs reconciliation")
        from app.pagebuilder.jobs import EDIT_RECEIPTS
        if EDIT_RECEIPTS.get() is not None:
            with db()() as session:
                begin = metered._model_session_begin(session, key)
                completed = metered._read_model_event(session, key, "completed", begin)
            if completed is None:
                raise ProviderCallError("provider_outcome_unknown", "Firstlook settlement is unconfirmed.")
            EDIT_RECEIPTS.get().append({**completed["usage"], "credits_micro": saved["credits_micro"], "settled": True})
        return (output if saved["output_usable"] else None, saved["credits_micro"] / MICRO_PER_USD)

    try:
        recovered = metered.recover_model_call(key)
    except Exception as exc:
        raise ProviderCallError("provider_outcome_unknown",
            "The saved Firstlook outcome needs reconciliation.") from exc
    if recovered is not None:
        return recovered_output(recovered)
    # A later onboarding attempt cannot authorize a fresh call when its original
    # receipt is absent. The original step key owns both fixed call slots.
    generation_call_id(generation_intent, workspace_id=wid, venture_id=venture_id,
        slot=call_slot, for_dispatch=True)
    founder_provider.register_workspace_llm_resolver()
    resolved = founder_provider.resolve_llm_config(wid)
    byok = resolved.source == "founder_override"
    config = (founder_provider.to_provider_config(wid, resolved_config=resolved)
              if byok else load_provider_config_from_env())
    if config is None:
        raise CreditsUnavailable("firstlook_provider_config_unavailable")
    account = get_account(venture_id) if venture_id else account_for_workspace(wid)
    if account is not None:
        from app.credits.accounts import account_can_spend
        with db()() as session:
            allowed = (account_can_spend(session, account, wid, venture_id) if venture_id
                       else account.scope == "person" or account.scope == "venture" and account.workspace_id == wid)
        if not allowed:
            raise CreditsUnavailable("firstlook_credit_scope_mismatch")
    if not byok and account is None:
        raise CreditsUnavailable("firstlook_credit_account_required")
    if account is not None:
        from sqlalchemy import select
        from app.db.models import CreditReservation
        with db()() as session:
            pending = session.scalar(select(CreditReservation.id).where(
                CreditReservation.account_id == account.id, CreditReservation.workspace_id == wid,
                CreditReservation.purpose == PURPOSE, CreditReservation.status == "reconciling"))
        if pending is not None:
            raise ProviderCallError("provider_outcome_unknown", "The saved provider outcome is unconfirmed.")
    user = json.dumps({"idea": idea_text, "audience_hint": audience or "",
        "competitors": (research or {}).get("competitors") or [],
        "demand": (research or {}).get("demand") or "",
        **({"founder_name": founder_name[:80]} if founder_name else {}),
        **({"founder_instruction": instruction[:600]} if instruction else {})}, ensure_ascii=False)
    config, messages, schema, captured = capture_request([
        {"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        purpose=PURPOSE, config=config, request_id=request_id, max_tokens=900,
        response_schema={"type": "json_object"})

    def checkpoint(response, usage):
        output = None
        if usage.get("provider_receipt") is not None and not usage.get("provider_output_error"):
            try:
                text = _text_of(response)
                parsed = _parse_json(text) if len(text) <= 64_000 and not looks_like_secret(text) else None
                output = validate_generation_output(_normalize(parsed, idea_text)) if parsed is not None else None
            except (TypeError, ValueError, AttributeError, CreditsUnavailable):
                pass
        checkpoint_generation_output(generation_intent, workspace_id=wid,
            venture_id=venture_id, slot=call_slot, output=output)

    provider_call = partial(sole.transmit, messages, purpose=PURPOSE,
        tier=config.captured_tier, max_tokens=captured["model_binding"]["max_output_tokens"], workspace_id=wid,
        config=config, response_schema=schema, use_workspace_override=False,
        single_attempt=True)

    async def dispatch():
        with sole.workspace_llm_context(wid):
            return await asyncio.wait_for(provider_call(), timeout=30.0)

    def admit_dispatch():
        generation_call_id(generation_intent, workspace_id=wid, venture_id=venture_id,
            slot=call_slot, for_dispatch=True)

    try:
        await metered.call(dispatch, workspace_id=wid, venture_id=venture_id,
            account_id=account.id if account else None, purpose=PURPOSE,
            model=captured["model_binding"]["deployment"], provider=config.provider,
            payer="founder" if byok else None, request_id=request_id,
            estimate_micro=captured["estimate_micro"], max_tokens=captured["token_ceiling"],
            model_binding=captured["model_binding"], request_digest=captured["request_digest"],
            before_model_receipt=checkpoint, before_dispatch=admit_dispatch)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            recovered = metered.recover_model_call(key)
        except Exception as recovery_error:
            raise ProviderCallError("provider_outcome_unknown",
                "The saved Firstlook outcome needs reconciliation.") from recovery_error
        if recovered is not None and recovered["outcome"] == "confirmed":
            return recovered_output(recovered)
        if recovered is not None and recovered["outcome"] == "rejected":
            if isinstance(exc, ProviderCallError):
                exc.unspent_confirmed = True
            raise
        raise
    recovered = metered.recover_model_call(key)
    if recovered is None:
        raise ProviderCallError("provider_outcome_unknown", "Firstlook settlement is unconfirmed.")
    return recovered_output(recovered)


async def generate_copy(
    idea_text: str,
    *,
    audience: str | None = None,
    allow_model: bool = True,
    research: dict[str, Any] | None = None,
    name: str | None = None,
    instruction: str | None = None,
    founder_name: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    generation_intent: str | None = None,
) -> tuple[dict[str, Any], float, bool]:
    """Return (copy, cost_usd, used_fallback).

    On lint failure: regenerate once, then plain fallback.
    """
    if not allow_model or _force_fallback():
        copy = fallback_copy(idea_text, audience=audience, research=research, name=name)
        return copy, 0.0, True

    cost = 0.0
    first, cost = await _call_model(idea_text, audience=audience, research=research, instruction=instruction,
                                    founder_name=founder_name, workspace_id=workspace_id, venture_id=venture_id,
                                    generation_intent=generation_intent, call_slot="first")
    if first is not None:
        result = lint_copy(first, idea_text=idea_text)
        if result.ok:
            return first, cost, False
        # One regenerate.
        second, cost2 = await _call_model(idea_text, audience=audience, research=research, instruction=instruction,
                                         founder_name=founder_name, workspace_id=workspace_id, venture_id=venture_id,
                                         generation_intent=generation_intent, call_slot="lint-regenerate")
        cost += cost2
        if second is not None and lint_copy(second, idea_text=idea_text).ok:
            return second, cost, False

    plain = fallback_copy(idea_text, audience=audience, research=research, name=name)
    return plain, cost, True


__all__ = [
    "PURPOSE",
    "TIER",
    "COPY_MODE_ENV",
    "HEADLINE_BUDGET",
    "copy_hash",
    "fallback_copy",
    "generate_copy",
    "third_person",
    "quiz_option_labels",
    "strip_quiz_leaks",
]

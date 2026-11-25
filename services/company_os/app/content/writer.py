"""Authoring prompts and explicit manual draft utilities.

Metered writer/critic/repair runs live in content.generation. Retired direct
model entry points fail closed so scheduling cannot bypass scoped receipts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.content.safety.linter import LintResult, rules_for

MAX_ATTEMPTS = 3
#: Below this the critic's verdict blocks publication.
CRITIC_FLOOR = 0.62

WRITER_SYSTEM = (
    "You write short posts as a founder talking to one specific buyer. You are "
    "concrete and plain. You never open with a rhetorical question, never use "
    "listicle scaffolding, and never state a number that was not given to you. "
    "Never invent a conversation, quote, user feedback, founder experience or timeline to pad a post. "
    "A claim that you are talking to customers or keep hearing feedback needs an exact statement in the brief claims or proof. "
    "Otherwise ask an honest workflow question. Do not invent a link in your profile."
)

CRITIC_SYSTEM = (
    "You are a skeptical reader who did not write this post and has no stake in "
    "publishing it. You judge whether it would survive a real feed: whether it "
    "says something specific, whether it reads as machine-written, and whether "
    "every factual assertion is supported by the supplied brief."
)

PLATFORM_GUIDANCE: dict[str, str] = {
    "linkedin": (
        "900 to 1600 characters. Open with a concrete situation in the first "
        "140 characters, before the 'see more' fold. Short paragraphs of one or "
        "two sentences. No hashtags beyond two. Keep the post self-contained without a URL in the body."
    ),
    "x": (
        "Under 270 characters, one idea, no thread. Keep the post self-contained without a URL. No hashtags. It must stand alone."
    ),
    "reddit": (
        "200 to 900 characters of honest discussion or customer discovery. Write as the founder "
        "researching this audience, never impersonate a customer or invent first-hand experience. "
        "Keep an unvalidated concept explicitly unvalidated. You may ask about the reader's actual workflow; "
        "do not name or pitch the product, include a signup link, or solicit a purchase. "
        "Do not pad a thin brief with a story. Plain markdown, no headers or bullet-point marketing."
    ),
}

_DEFAULT_GUIDANCE = "Plain text, one idea, no marketing scaffolding."


def editorial_contract(platform: str, *, intent: str = "value", product_url: str = "") -> dict[str, Any]:
    """One shared format contract for author and reviewer; guidance is not evidence."""
    rules = rules_for(platform, intent=intent)
    guidance = PLATFORM_GUIDANCE.get(platform, _DEFAULT_GUIDANCE)
    if platform == "linkedin" and intent == "promo":
        guidance = (
            "250 to 700 characters. State the current product stage plainly. "
            "Do not invent founder or customer anecdotes to add length. "
            "One link to the exact product URL in the brief is allowed when supplied; "
            "otherwise write a linkless post."
        )
    return {
        "publication_mode": "text_only",
        "visual": "must_be_null; absence is required, not a quality defect",
        "title": "required_nonempty" if platform == "reddit" else "must_be_empty; absence is required, not a quality defect",
        "platform_guidance": guidance,
        "approved_conversion_url": product_url if platform == "linkedin" and intent == "promo" else "",
        "hard_limits": {"max_chars": rules.max_chars, "min_chars": rules.min_chars,
                        "max_links": rules.max_links, "max_hashtags": rules.max_hashtags},
    }


GROUNDING_RULES = [
    "Every factual or implied product capability must be supported by the supplied brief, not by its category, an audience pain, a topic, or repair feedback.",
    "Preserve all supplied limits and stage qualifiers: an unbuilt concept or proposed capability must remain proposed, not available or proven.",
    "If shipping status or availability is not stated, do not assume the product is live; use clearly proposed wording or omit the availability claim.",
    "Do not turn manual review into automatic acceptance, or a scheduling idea into a guarantee against double-booking, unless the brief explicitly supports that exact claim.",
    "Never invent customers, results, integrations, automation, reliability guarantees, availability, or founder experience.",
    "Never invent conversations, quotes, interviews, feedback, or elapsed timelines to make a thin brief sound like a personal story. A short factual post is better.",
    "If support is absent or ambiguous, omit the claim or clearly frame it as a proposal/question for research; do not imply an existing feature or observed outcome.",
    "Founder-supplied claims are reported input, not independently verified evidence. Advice and hypothetical examples must remain distinguishable from product facts.",
]


@dataclass(slots=True)
class Draft:
    body: str
    title: str | None = None
    visual: dict[str, Any] = field(default_factory=dict)
    lint: LintResult | None = None
    critic: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    ok: bool = False
    refusal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "title": self.title,
            "visual": self.visual,
            "ok": self.ok,
            "attempts": self.attempts,
            "lint": self.lint.to_dict() if self.lint else {},
            "critic": self.critic,
            "refusal_reason": self.refusal_reason,
        }


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_json(text: str) -> Any:
    body = (text or "").strip()
    if body.startswith("```"):
        parts = body.split("```")
        body = parts[1] if len(parts) > 1 else body.strip("`")
        body = body.removeprefix("json").strip()
    start = body.find("{")
    if start > 0:
        body = body[start:]
    return json.loads(body)


def _writer_prompt(
    *,
    topic: dict[str, Any],
    brief: dict[str, Any],
    platform: str,
    feedback: list[str],
) -> dict[str, Any]:
    intent = topic.get("intent") or "value"
    product_url = str((brief.get("product") or {}).get("url") or "").strip()
    contract = editorial_contract(platform, intent=intent, product_url=product_url)
    rules = rules_for(platform, intent=intent)
    return {
        "task": (
            "Write one text-only post. Return JSON: "
            '{"body": "...", "title": "...", "visual": null}. '
            "Only text publication is supported. Set visual to null; do not generate visual templates or image descriptions."
        ),
        "platform": platform,
        "platform_rules": contract["platform_guidance"],
        "hard_limits": {
            "max_chars": rules.max_chars,
            "min_chars": rules.min_chars,
            "max_links": rules.max_links,
            "max_hashtags": rules.max_hashtags,
        },
        "topic": topic,
        "audience": topic.get("icp_segment") or "",
        "voice": brief.get("voice") or {},
        "product": brief.get("product") or {},
        "permitted_claims": brief.get("claims") or [],
        "editorial_contract": contract,
        "grounding_rules": GROUNDING_RULES,
        "constraints": [
            "Every number you write must appear in permitted_claims or product.",
            "Title is only used on Reddit; send an empty string elsewhere.",
            "Do not write 'as an AI', do not describe your own process.",
            "No em-dash-heavy cadence, no tricolon openers, no 'It's not X, it's Y'.",
            "visual must be null because visual assets are unsupported.",
        ],
        "fix_these": feedback,
    }


async def _generate(
    *, topic: dict, brief: dict, platform: str, feedback: list[str], workspace_id: str
) -> tuple[str, str | None, dict]:
    raise RuntimeError("content_scoped_generation_required: use the company generation quote and durable job API")


async def critique(
    *, body: str, topic: dict, brief: dict, platform: str, workspace_id: str
) -> dict[str, Any]:
    raise RuntimeError("content_scoped_generation_required: use the company generation quote and durable job API")


def template_draft(topic: dict[str, Any], brief: dict[str, Any], platform: str) -> str:
    """A plain draft assembled from the brief when no model provider is set.

    Only founder-supplied sentences go in. The topic's ``rationale`` is the
    planner talking to itself and must never reach a post body.
    """
    hook = str(topic.get("hook") or topic.get("angle") or "").strip().rstrip(".")
    segment = str(topic.get("icp_segment") or "").strip()
    product = brief.get("product") or {}
    differentiators = [str(d).strip().rstrip(".") for d in product.get("differentiators") or []]
    one_liner = str(product.get("one_liner") or "").strip()
    rules = rules_for(platform)

    lines: list[str] = []
    if hook:
        lines.append(f"{hook}.")
    # No fixed connector sentence: shared scaffolding across drafts is exactly
    # what the near-duplicate gate exists to catch.

    if topic.get("intent") == "promo":
        if differentiators:
            lines.append(differentiators[0] + ".")
        elif one_liner:
            lines.append(one_liner)
    else:
        # Value posts stay off the product entirely, so pull supporting detail
        # from the segment the angle came from.
        for seg in (brief.get("icp") or {}).get("segments") or []:
            if seg.get("name") != segment:
                continue
            extra = [str(p).strip().rstrip(".") for p in seg.get("pains") or []]
            for pain in extra:
                if pain and pain.lower() != hook.lower():
                    lines.append(f"It usually shows up alongside: {pain}.")
                    break
            break

    body = "\n\n".join(line for line in lines if line)
    return body[: rules.max_chars].strip()


async def compose(
    *,
    topic: dict[str, Any],
    brief: dict[str, Any],
    platform: str,
    workspace_id: str,
    subreddit_rules: list[str] | None = None,
) -> Draft:
    raise RuntimeError("content_scoped_generation_required: use the company generation quote and durable job API")

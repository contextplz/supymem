"""Slate generation: retrieve → cluster → compose → filter → copy (37 §4).

Two calls at most, both optional. The retrieved set and the composed pool are
cached by ``seed_hash``, so a reroll costs a single Flash call and the same seed
always produces the same slate. When no model is reachable the composer falls
back to the corpus's own words — never to an invented claim.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from llm_gateway import sole

from app.idealab import corpus as corpus_mod
from app.idealab import filters
from app.idealab.corpus import Cluster, CorpusItem
from app.idealab.filters import Candidate, Drop

SLATE_SIZE = 3
COMPOSE_CANDIDATES = 5
COST_CAP_USD = 0.05
WALL_CLOCK_S = 45.0

COMPOSE_TIER = "pro"
COPY_TIER = "flash"

# Used only when a provider answers without a usage block.
_EST_TOKENS: dict[str, tuple[int, int]] = {"pro": (3000, 900), "flash": (1500, 400)}

_FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.M)

_CACHE: dict[str, "Retrieval"] = {}


@dataclass(frozen=True)
class Seed:
    """Everything is optional: Surprise Me is never gated behind a form (37 §3)."""

    skills: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    posture: str = "nights_and_weekends"
    geography: str = ""
    tolerance: str = "safe_bet"

    @classmethod
    def from_input(cls, data: Mapping[str, Any] | None) -> Seed:
        data = data or {}
        return cls(
            skills=_chips(data.get("skills")),
            interests=_chips(data.get("interests")),
            posture=_posture(data.get("posture")),
            geography=str(data.get("geography") or "").strip(),
            tolerance=_tolerance(data.get("tolerance")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": list(self.skills),
            "interests": list(self.interests),
            "posture": self.posture,
            "geography": self.geography,
            "tolerance": self.tolerance,
        }

    def tokens(self) -> list[str]:
        parts = [*self.skills, *self.interests]
        if self.geography:
            parts.append(self.geography)
        return [p.lower() for p in parts]

    @property
    def hash(self) -> str:
        import hashlib

        blob = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Retrieval:
    """The per-seed cache. Round 2+ reads this instead of paying again."""

    items: tuple[CorpusItem, ...]
    clusters: tuple[Cluster, ...]
    corpus_fresh: bool
    pool: list[Candidate] = field(default_factory=list)
    composed_with_model: bool = False


@dataclass(frozen=True)
class Slate:
    seed: Seed
    round: int
    opportunities: list[Candidate]
    dropped: list[Drop]
    cost_usd: float
    researched: bool
    duration_s: float
    reason: str = ""


def _chips(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, Sequence):
        raw = [str(v) for v in value]
    else:
        return ()
    return tuple(sorted({v.strip() for v in raw if v and v.strip()}))


def _posture(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in filters.POSTURES else "nights_and_weekends"


def _tolerance(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "swing" if text == "swing" else "safe_bet"


def reset_cache() -> None:
    _CACHE.clear()


def discovery_options(proposed_options) -> list[str]:
    from core.copy import no_dashes

    options = [no_dashes(f"{option.offer} for {option.customer}") for option in proposed_options
               if option.offer.strip() and option.customer.strip()]
    if len(set(options)) != 3 or any(len(option) > 300 for option in options):
        raise ValueError("discovery_options_invalid")
    return options


def offline() -> bool:
    raw = os.environ.get("LLM_OFFLINE")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes"}
    from app.config import settings

    return bool(settings.llm_offline)


# --- cost -----------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


@lru_cache(maxsize=1)
def _prices() -> dict[str, dict[str, float]]:
    import yaml

    with (_repo_root() / "config" / "pricing.yaml").open() as handle:
        data = yaml.safe_load(handle) or {}
    return data.get("tiers") or {}


def price(tier: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = _prices().get(tier) or {}
    per_in = float(rates.get("input") or 0.0) / 1_000_000
    per_out = float(rates.get("output") or 0.0) / 1_000_000
    return prompt_tokens * per_in + completion_tokens * per_out


def estimated_cost(tier: str) -> float:
    prompt, completion = _EST_TOKENS.get(tier, (1500, 400))
    return price(tier, prompt, completion)


def _usage_cost(tier: str, response: Any) -> float:
    usage = _attr(response, "usage")
    prompt = int(_attr(usage, "prompt_tokens") or 0)
    completion = int(_attr(usage, "completion_tokens") or 0)
    if not prompt and not completion:
        return estimated_cost(tier)
    return price(tier, prompt, completion)


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _text_of(response: Any) -> str:
    choices = _attr(response, "choices") or []
    if not choices:
        return ""
    message = _attr(choices[0], "message")
    return str(_attr(message, "content") or "")


def _parse_json(text: str) -> Any:
    cleaned = _FENCE_RE.sub("", text or "").strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


# --- composition ----------------------------------------------------------


_COMPOSE_SYSTEM = (
    "You compose startup opportunities for a solo founder. Use only the signals "
    "given to you. Every evidence claim you return must be copied verbatim from "
    "one of those signals. Never invent a market size, a user count, a customer "
    "quote, or a source. Answer with JSON only."
)

_COPY_SYSTEM = (
    "You write plain, specific product one-liners. No buzzwords, no numbers you "
    "were not given, no market sizes. Answer with JSON only."
)


def _compose_prompt(seed: Seed, clusters: Sequence[Cluster], constraints: Sequence[str]) -> str:
    signals = []
    for cluster in clusters:
        for item in cluster.items:
            signals.append(
                {
                    "cluster": cluster.key,
                    "claim": item.claim,
                    "kind": item.kind,
                    "audience": item.audience,
                    "geography": item.geography,
                    "need": item.need,
                }
            )
    body = {
        "seed": seed.to_dict(),
        "signals": signals,
        "want": COMPOSE_CANDIDATES,
        "avoid": list(constraints),
        "fields": [
            "title",
            "one_liner",
            "first_customer",
            "wedge",
            "why_now",
            "kill_criteria",
            "shape_tags",
            "effort_days",
            "evidence",
        ],
    }
    return json.dumps(body, sort_keys=True)


def _verified_evidence(
    raw: Any, retrieved: Sequence[CorpusItem], *, corpus_fresh: bool
) -> list[dict[str, Any]]:
    """Keep only claims that came from the cache. Sources are never the model's."""
    by_hash = {item.id: item for item in retrieved}
    normalized = [(corpus_mod.content_hash(i.claim), i) for i in retrieved]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw or ():
        claim = str(_attr(entry, "claim") or "").strip()
        if not claim:
            continue
        digest = corpus_mod.content_hash(claim)
        item = by_hash.get(digest)
        if item is None:
            low = claim.lower()
            item = next(
                (i for _, i in normalized if low in i.claim.lower() or i.claim.lower() in low),
                None,
            )
        if item is not None:
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item.as_evidence())
        elif not corpus_fresh:
            # Nothing to check against: keep the reasoning, stamp it as such.
            out.append(
                {"claim": claim, "source": "", "kind": "model_prior", "retrieved_at": ""}
            )
    return out[:4]


def _candidate_from_model(
    entry: Any, retrieved: Sequence[CorpusItem], *, corpus_fresh: bool
) -> Candidate | None:
    title = str(_attr(entry, "title") or "").strip()
    one_liner = str(_attr(entry, "one_liner") or "").strip()
    first_customer = str(_attr(entry, "first_customer") or "").strip()
    wedge = str(_attr(entry, "wedge") or "").strip()
    why_now = str(_attr(entry, "why_now") or "").strip()
    kill_criteria = str(_attr(entry, "kill_criteria") or "").strip()
    if not all([title, one_liner, first_customer, wedge, why_now, kill_criteria]):
        return None
    evidence = _verified_evidence(
        _attr(entry, "evidence"), retrieved, corpus_fresh=corpus_fresh
    )
    tags = [str(t).strip().lower() for t in (_attr(entry, "shape_tags") or ()) if str(t).strip()]
    try:
        effort = int(_attr(entry, "effort_days") or 30)
    except (TypeError, ValueError):
        effort = 30
    return Candidate(
        title=title[:120],
        one_liner=one_liner[:filters.ONE_LINER_MAX],
        first_customer=first_customer[:160],
        wedge=wedge[:200],
        why_now=why_now[:240],
        kill_criteria=kill_criteria[:240],
        evidence=evidence,
        shape_tags=sorted(set(tags))[:6],
        effort_days=max(1, effort),
        confidence=_confidence(evidence, corpus_fresh=corpus_fresh),
        cluster_key=str(_attr(entry, "cluster") or ""),
    )


def _confidence(evidence: Sequence[Mapping[str, Any]], *, corpus_fresh: bool) -> float:
    """Calibrated low on purpose: an opportunity is a hypothesis (12 §6)."""
    if not corpus_fresh:
        return 0.25
    sourced = len(filters.sourced_evidence(evidence))
    return round(min(0.35 + 0.1 * min(sourced, 3), 0.65), 2)


def _candidate_from_cluster(cluster: Cluster, seed: Seed) -> Candidate | None:
    """Compose from the cache's own words. Skips a cluster it cannot describe."""
    audience = next((i.audience for i in cluster.items if i.audience), None)
    need = next((i.need for i in cluster.items if i.need), None)
    if not audience or not need:
        return None
    geography = next((i.geography for i in cluster.items if i.geography), seed.geography)
    changed = next(
        (i for i in cluster.items if i.kind == "platform_change"),
        max(cluster.items, key=lambda i: (i.retrieved_at, i.id)),
    )
    first_customer = f"{audience} in {geography}" if geography else audience
    evidence = [i.as_evidence() for i in cluster.items][:4]
    tags = sorted({*cluster.tags, cluster.category})[:6]
    return Candidate(
        title=f"{need[:1].upper()}{need[1:]}"[:120],
        one_liner=f"{need[:1].upper()}{need[1:]} for {first_customer}."[
            : filters.ONE_LINER_MAX
        ],
        first_customer=first_customer[:160],
        wedge=f"{need}, sold to one {audience.rstrip('s')} at a time"[:200],
        why_now=changed.claim[:240],
        kill_criteria=(
            f"Wrong if {audience} already cover {need} with what they have."
        )[:240],
        evidence=evidence,
        shape_tags=tags,
        effort_days=14 + 7 * min(len(cluster.items), 4),
        confidence=_confidence(evidence, corpus_fresh=True),
        cluster_key=cluster.key,
    )


async def _compose_with_model(
    seed: Seed,
    clusters: Sequence[Cluster],
    retrieved: Sequence[CorpusItem],
    *,
    corpus_fresh: bool,
    constraints: Sequence[str] = (),
) -> tuple[list[Candidate], float]:
    messages = [
        {"role": "system", "content": _COMPOSE_SYSTEM},
        {"role": "user", "content": _compose_prompt(seed, clusters, constraints)},
    ]
    try:
        response = await sole.env_chat_completion(messages, tier=COMPOSE_TIER)
    except Exception:  # noqa: BLE001 — no slate is worth an exception to the founder
        return [], 0.0
    cost = _usage_cost(COMPOSE_TIER, response)
    parsed = _parse_json(_text_of(response))
    entries = parsed.get("candidates") if isinstance(parsed, Mapping) else parsed
    if not isinstance(entries, list):
        return [], cost
    out: list[Candidate] = []
    for entry in entries:
        cand = _candidate_from_model(entry, retrieved, corpus_fresh=corpus_fresh)
        if cand is not None:
            out.append(cand)
    return out, cost


async def _write_copy(candidates: Sequence[Candidate]) -> tuple[float, int]:
    """One Flash call for the survivors' words. Bad copy keeps the original."""
    payload = json.dumps(
        {
            "cards": [
                {
                    "wedge": c.wedge,
                    "first_customer": c.first_customer,
                    "why_now": c.why_now,
                }
                for c in candidates
            ]
        },
        sort_keys=True,
    )
    messages = [
        {"role": "system", "content": _COPY_SYSTEM},
        {"role": "user", "content": payload},
    ]
    try:
        response = await sole.env_chat_completion(messages, tier=COPY_TIER)
    except Exception:  # noqa: BLE001
        return 0.0, 0
    cost = _usage_cost(COPY_TIER, response)
    parsed = _parse_json(_text_of(response))
    entries = parsed.get("cards") if isinstance(parsed, Mapping) else parsed
    if not isinstance(entries, list):
        return cost, 0
    rewritten = 0
    for cand, entry in zip(candidates, entries):
        title = str(_attr(entry, "title") or "").strip()
        one_liner = str(_attr(entry, "one_liner") or "").strip()
        if filters.copy_is_usable(title, one_liner):
            cand.title = title[:120]
            cand.one_liner = one_liner[: filters.ONE_LINER_MAX]
            rewritten += 1
    return cost, rewritten


# --- the entry point ------------------------------------------------------


def _retrieval(seed: Seed) -> Retrieval:
    cached = _CACHE.get(seed.hash)
    if cached is not None:
        return cached
    loaded = corpus_mod.load()
    items = tuple(corpus_mod.retrieve(seed.tokens(), corpus=loaded))
    retrieval = Retrieval(
        items=items,
        clusters=tuple(corpus_mod.cluster(items)),
        corpus_fresh=loaded.fresh(),
    )
    _CACHE[seed.hash] = retrieval
    return retrieval


def _order(pool: Sequence[Candidate], seen_keys: Sequence[str]) -> list[Candidate]:
    """Unseen first. A card the founder saw but did not reject may come back."""
    seen = set(seen_keys)
    return [c for c in pool if c.key not in seen] + [c for c in pool if c.key in seen]


async def generate_slate(
    seed: Seed,
    *,
    round: int = 1,
    rejected_shapes: Sequence[frozenset[str]] = (),
    seen_keys: Sequence[str] = (),
    budget_usd: float = COST_CAP_USD,
) -> Slate:
    started = time.monotonic()
    spent = 0.0
    with corpus_mod.generating():
        retrieval = _retrieval(seed)
        allow_model = not offline()

        if not retrieval.pool:
            if allow_model and budget_usd - spent >= estimated_cost(COMPOSE_TIER):
                composed, cost = await _compose_with_model(
                    seed,
                    retrieval.clusters,
                    retrieval.items,
                    corpus_fresh=retrieval.corpus_fresh,
                )
                spent += cost
                retrieval.pool = composed
                retrieval.composed_with_model = bool(composed)
            if not retrieval.pool:
                retrieval.pool = [
                    c
                    for c in (
                        _candidate_from_cluster(cl, seed) for cl in retrieval.clusters
                    )
                    if c is not None
                ]

        outcome = filters.apply(
            _order(retrieval.pool, seen_keys),
            posture=seed.posture,
            rejected_shapes=rejected_shapes,
            corpus_fresh=retrieval.corpus_fresh,
        )
        survivors = outcome.kept[:SLATE_SIZE]
        dropped = list(outcome.dropped)

        # Fewer than three survivors: regenerate once with the failures as
        # constraints, then show what survived. Never pad the slate (37 §5).
        if (
            len(survivors) < SLATE_SIZE
            and allow_model
            and round == 1
            and retrieval.composed_with_model
            and budget_usd - spent >= estimated_cost(COMPOSE_TIER)
        ):
            constraints = sorted({d.detail for d in outcome.dropped})
            extra, cost = await _compose_with_model(
                seed,
                retrieval.clusters,
                retrieval.items,
                corpus_fresh=retrieval.corpus_fresh,
                constraints=constraints,
            )
            spent += cost
            known = {c.key for c in retrieval.pool}
            retrieval.pool.extend(c for c in extra if c.key not in known)
            outcome = filters.apply(
                _order(retrieval.pool, seen_keys),
                posture=seed.posture,
                rejected_shapes=rejected_shapes,
                corpus_fresh=retrieval.corpus_fresh,
            )
            survivors = outcome.kept[:SLATE_SIZE]
            dropped = list(outcome.dropped)

        if survivors and allow_model and budget_usd - spent >= estimated_cost(COPY_TIER):
            cost, _ = await _write_copy(survivors)
            spent += cost

    researched = bool(survivors) and retrieval.corpus_fresh and all(
        c.researched for c in survivors
    )
    reason = "" if survivors else "no_evidence"
    return Slate(
        seed=seed,
        round=round,
        opportunities=survivors,
        dropped=dropped,
        cost_usd=round_cost(spent),
        researched=researched,
        duration_s=time.monotonic() - started,
        reason=reason,
    )


def round_cost(value: float) -> float:
    return float(f"{value:.6f}")

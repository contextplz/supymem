"""Local evidence cache for the Idea Lab (37 §4).

Generation reads only from this cache. Fetching is a separate background job
with its own budget: a slate must never pay for a fetch inside its 45 s path,
so :func:`refresh` refuses to run while a generation is in flight.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from core.clock import as_utc, utcnow

# Refreshed at most daily; anything older is treated as stale (37 §4).
MAX_AGE_S = 86_400
REFRESH_BUDGET_USD = 0.02
RETRIEVE_LIMIT = 40

# Kinds that count as research. `model_prior` is what an opportunity gets when
# the cache is empty or stale, and it is never presented as a source.
RESEARCHED_KINDS = frozenset(
    {"job_posting", "pricing_page", "platform_change", "complaint_thread"}
)
KINDS = RESEARCHED_KINDS | {"model_prior"}

SignalSource = Callable[[], Sequence["CorpusItem"]]

_IN_GENERATION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "idealab_generating", default=False
)


class RefreshInsideGeneration(RuntimeError):
    """A corpus refresh was attempted from inside a slate generation."""


@dataclass(frozen=True)
class CorpusItem:
    """One cached signal. `source` is where a founder can go and check it."""

    claim: str
    source: str
    kind: str
    category: str
    retrieved_at: str
    tags: tuple[str, ...] = ()
    audience: str | None = None
    geography: str | None = None
    # What the signal implies is missing. Declared at ingest rather than guessed
    # from the claim text, because a guessed need is an invented one.
    need: str | None = None

    @property
    def id(self) -> str:
        return content_hash(self.claim)

    @property
    def researched(self) -> bool:
        return self.kind in RESEARCHED_KINDS and bool(self.source.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source": self.source,
            "kind": self.kind,
            "category": self.category,
            "retrieved_at": self.retrieved_at,
            "tags": list(self.tags),
            "audience": self.audience,
            "geography": self.geography,
            "need": self.need,
        }

    def as_evidence(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source": self.source,
            "kind": self.kind,
            "retrieved_at": self.retrieved_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CorpusItem:
        claim = str(data.get("claim") or "").strip()
        kind = str(data.get("kind") or "").strip()
        if not claim or kind not in KINDS:
            raise ValueError(f"unusable corpus row: {dict(data)!r}")
        tags = tuple(sorted({str(t).strip().lower() for t in data.get("tags") or () if str(t).strip()}))
        return cls(
            claim=claim,
            source=str(data.get("source") or "").strip(),
            kind=kind,
            category=str(data.get("category") or "general").strip().lower(),
            retrieved_at=str(data.get("retrieved_at") or "").strip(),
            tags=tags,
            audience=(str(data["audience"]).strip() if data.get("audience") else None),
            geography=(str(data["geography"]).strip() if data.get("geography") else None),
            need=(str(data["need"]).strip() if data.get("need") else None),
        )


@dataclass(frozen=True)
class Cluster:
    """Signals that point at the same opening, grouped deterministically."""

    key: str
    category: str
    tags: tuple[str, ...]
    items: tuple[CorpusItem, ...]

    @property
    def researched(self) -> bool:
        return any(i.researched for i in self.items)


@dataclass(frozen=True)
class Corpus:
    items: tuple[CorpusItem, ...] = ()
    refreshed_at: str | None = None

    def fresh(self) -> bool:
        if not self.items or not self.refreshed_at:
            return False
        stamp = _parse_stamp(self.refreshed_at)
        if stamp is None:
            return False
        return (utcnow() - stamp) <= timedelta(seconds=MAX_AGE_S)

    def to_dict(self) -> dict[str, Any]:
        return {
            "refreshed_at": self.refreshed_at,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass(frozen=True)
class RefreshReport:
    added: int = 0
    duplicates: int = 0
    rejected: int = 0
    cost_usd: float = 0.0
    ran: bool = True
    reason: str = ""


def content_hash(claim: str) -> str:
    """The same claim from two pages counts once (37 §4)."""
    normalized = " ".join(claim.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _parse_stamp(value: str):
    from datetime import datetime

    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def data_dir() -> Path:
    override = os.environ.get("IDEALAB_DATA_DIR")
    return Path(override) if override else _repo_root() / "data" / "idealab"


def corpus_path() -> Path:
    return data_dir() / "corpus.json"


def incoming_dir() -> Path:
    return data_dir() / "incoming"


@contextmanager
def generating() -> Iterator[None]:
    """Mark the cheap path so a fetch cannot sneak into it."""
    token = _IN_GENERATION.set(True)
    try:
        yield
    finally:
        _IN_GENERATION.reset(token)


def in_generation() -> bool:
    return _IN_GENERATION.get()


def load() -> Corpus:
    path = corpus_path()
    if not path.is_file():
        return Corpus()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Corpus()
    items: list[CorpusItem] = []
    for row in raw.get("items") or ():
        try:
            items.append(CorpusItem.from_dict(row))
        except ValueError:
            continue
    return Corpus(items=tuple(items), refreshed_at=raw.get("refreshed_at"))


def save(corpus: Corpus) -> None:
    path = corpus_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(corpus.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def record(items: Iterable[CorpusItem | Mapping[str, Any]]) -> RefreshReport:
    """Merge rows into the cache, dropping duplicate claims and bad rows."""
    corpus = load()
    seen = {i.id for i in corpus.items}
    kept = list(corpus.items)
    added = duplicates = rejected = 0
    now = utcnow().isoformat()
    for raw in items:
        try:
            item = raw if isinstance(raw, CorpusItem) else CorpusItem.from_dict(raw)
        except ValueError:
            rejected += 1
            continue
        if item.kind in RESEARCHED_KINDS and not item.source.strip():
            rejected += 1
            continue
        if item.id in seen:
            duplicates += 1
            continue
        seen.add(item.id)
        kept.append(item if item.retrieved_at else replace(item, retrieved_at=now))
        added += 1
    if added:
        save(Corpus(items=tuple(kept), refreshed_at=now))
    return RefreshReport(added=added, duplicates=duplicates, rejected=rejected)


def drop_dir_source() -> list[CorpusItem]:
    """Signals the fetcher left behind as JSON files, read locally and cheaply."""
    folder = incoming_dir()
    if not folder.is_dir():
        return []
    out: list[CorpusItem] = []
    for path in sorted(folder.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = raw.get("items") if isinstance(raw, Mapping) else raw
        for row in rows or ():
            try:
                out.append(CorpusItem.from_dict(row))
            except ValueError:
                continue
    return out


def refresh(
    *,
    sources: Sequence[SignalSource] | None = None,
    budget_usd: float = REFRESH_BUDGET_USD,
    force: bool = False,
) -> RefreshReport:
    """Background job. Never called from generation; raises if it is."""
    if in_generation():
        raise RefreshInsideGeneration(
            "corpus refresh runs as its own job, not inside slate generation (37 §4)"
        )
    corpus = load()
    if not force and corpus.fresh():
        return RefreshReport(ran=False, reason="fresh")
    if budget_usd < 0:
        return RefreshReport(ran=False, reason="no_budget")
    collected: list[CorpusItem] = []
    for source in sources if sources is not None else (drop_dir_source,):
        try:
            collected.extend(source())
        except Exception:  # noqa: BLE001 — one bad source must not stop the job
            continue
    report = record(collected)
    return replace(report, cost_usd=0.0, ran=True, reason="collected")


def _score(item: CorpusItem, tokens: Sequence[str]) -> int:
    if not tokens:
        return 0
    haystack = " ".join(
        [item.claim.lower(), item.category, " ".join(item.tags), (item.geography or "").lower()]
    )
    return sum(1 for token in tokens if token and token in haystack)


def retrieve(
    tokens: Sequence[str] = (),
    *,
    limit: int = RETRIEVE_LIMIT,
    corpus: Corpus | None = None,
) -> list[CorpusItem]:
    """Deterministic: score by seed overlap, break ties on the content hash."""
    corpus = corpus if corpus is not None else load()
    normalized = [t.strip().lower() for t in tokens if t and t.strip()]
    ranked = sorted(
        corpus.items,
        key=lambda i: (-_score(i, normalized), i.category, i.id),
    )
    return ranked[:limit]


def cluster(items: Sequence[CorpusItem]) -> list[Cluster]:
    """Group by category, then by the tag a category's signals share."""
    grouped: dict[tuple[str, str], list[CorpusItem]] = {}
    for item in items:
        anchor = item.tags[0] if item.tags else item.category
        grouped.setdefault((item.category, anchor), []).append(item)
    clusters: list[Cluster] = []
    for (category, anchor), members in sorted(grouped.items()):
        ordered = sorted(members, key=lambda i: (i.kind, i.id))
        tags = tuple(sorted({t for m in ordered for t in m.tags}))
        clusters.append(
            Cluster(
                key=f"{category}:{anchor}",
                category=category,
                tags=tags or (category,),
                items=tuple(ordered),
            )
        )
    return clusters

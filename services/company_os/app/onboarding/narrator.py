"""ActivityLine emission, interpolation, honesty variants, coalescing (26 §7 / 27)."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.core.eventbus import bus
from app.db.models import ActivityLine
from app.onboarding.failures import ALL_ERROR_CLASSES, ErrorClass
from app.state import db
from core.clock import utcnow

_DECK: dict[str, Any] | None = None
_TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

COALESCE_RULES: dict[tuple[str, str], tuple[str | None, int]] = {
    ("code", "read"): ("build.reading_files", 3),
    ("code", "edit"): ("build.editing_files", 2),
    ("code", "write"): ("build.writing_files", 2),
    ("code", "run"): ("build.running_commands", 3),
    ("search", "web"): (None, 0),
}

AUTONOMY_WORDS = {"L1": "Suggest", "L2": "Assist", "L3": "Act", "L4": "Autopilot"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def load_deck(force: bool = False) -> dict[str, Any]:
    """Load ``config/narration.yaml`` plus every ``config/narration.d/*.yaml``.

    Fragments let each feature own its copy without serialising edits through
    one file; keys must not collide, and a collision is loud rather than a
    silent last-writer-wins.
    """
    global _DECK
    if _DECK is not None and not force:
        return _DECK
    import yaml

    root = _repo_root() / "config"
    deck: dict[str, Any] = {}
    with (root / "narration.yaml").open() as f:
        deck.update(yaml.safe_load(f) or {})

    fragments = root / "narration.d"
    if fragments.is_dir():
        for path in sorted(fragments.glob("*.yaml")):
            with path.open() as f:
                extra = yaml.safe_load(f) or {}
            clashes = sorted(set(extra) & set(deck) - {"failures"})
            if clashes:
                raise ValueError(
                    f"narration key collision in {path.name}: {', '.join(clashes)}"
                )
            # `failures` is the one shared namespace; merge it rather than replace.
            merged_failures = {**deck.get("failures", {}), **extra.pop("failures", {})}
            deck.update(extra)
            if merged_failures:
                deck["failures"] = merged_failures
    _DECK = deck
    return _DECK


def interpolate(template: str, tokens: dict[str, Any] | None = None) -> str:
    """Replace {tokens}; missing tokens are removed (never leave {name})."""
    tokens = tokens or {}

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in tokens or tokens[key] is None:
            return ""
        return str(tokens[key])

    text = _TOKEN_RE.sub(repl, template)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = re.sub(r"\s+([.,])", r"\1", text)
    return text


def resolve_variant(
    key: str,
    *,
    status: str = "done",
    simulated: bool = False,
    downgraded: bool = False,
    partial: bool = False,
    error_class: str | None = None,
    demand: str | None = None,
) -> str:
    deck = load_deck()
    if status == "failed" and error_class:
        failures = deck.get("failures") or {}
        ec = ALL_ERROR_CLASSES.get(error_class)
        fk = ec.narration_key if ec else error_class
        entry = failures.get(fk) or failures.get("internal") or {}
        return entry.get("text") or "Something went wrong on my side. I saved your progress and logged it."

    entry = deck.get(key) or {}
    if isinstance(entry, str):
        return entry

    if demand and f"done_{demand}" in entry:
        return entry[f"done_{demand}"]
    if simulated and "done_simulated" in entry:
        return entry["done_simulated"]
    if downgraded and "done_preview" in entry:
        return entry["done_preview"]
    if downgraded and "done_draft" in entry:
        return entry["done_draft"]
    if partial and "done_partial" in entry:
        return entry["done_partial"]
    if status == "running" and "running" in entry:
        return entry["running"]
    if status == "blocked" and "blocked" in entry:
        return entry["blocked"]
    if status == "skipped" and "done" in entry:
        # prefer explicit skip keys handled by caller
        return entry.get("done") or entry.get("running") or ""
    return entry.get("done") or entry.get("running") or entry.get("blocked") or ""


class Narrator:
    """Single writer for activity_lines."""

    def __init__(self, venture_id: str, run_id: str | None = None):
        self.venture_id = venture_id
        self.run_id = run_id
        self._coalesce_buf: list[dict[str, Any]] = []
        self._coalesce_kind: tuple[str, str] | None = None
        self._coalesce_started: float = 0.0
        self._emit_times: list[float] = []
        self._phase = "orient"
        self.tokens: dict[str, Any] = {}

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def set_tokens(self, **kwargs: Any) -> None:
        self.tokens.update({k: v for k, v in kwargs.items() if v is not None})

    async def begin(self, step_id: str, *, kind: str = "step", tokens: dict | None = None) -> ActivityLine | None:
        text = resolve_variant(step_id, status="running")
        if not text:
            return None
        merged = {**self.tokens, **(tokens or {})}
        query = merged.get("query")
        # A search line without a query fails founder-facing honesty checks.
        emit_kind = kind if not (kind == "search" and not query) else "step"
        return await self.emit(
            phase=self._phase,
            kind=emit_kind,
            status="running",
            text=interpolate(text, merged),
            step_id=step_id,
            query=str(query)[:120] if query else None,
        )

    async def done(
        self,
        step_id: str,
        *,
        kind: str = "step",
        simulated: bool = False,
        downgraded: bool = False,
        partial: bool = False,
        detail: list[str] | None = None,
        query: str | None = None,
        evidence_url: str | None = None,
        undo: dict | None = None,
        cost_usd: float | None = None,
        tokens: dict | None = None,
        demand: str | None = None,
        variant_key: str | None = None,
    ) -> ActivityLine:
        key = variant_key or step_id
        text = resolve_variant(
            key,
            status="done",
            simulated=simulated,
            downgraded=downgraded,
            partial=partial,
            demand=demand,
        )
        merged = {**self.tokens, **(tokens or {})}
        if query:
            merged.setdefault("query", query)
        return await self.emit(
            phase=self._phase,
            kind=kind,
            status="done",
            text=interpolate(text, merged),
            step_id=step_id,
            detail=detail,
            query=query,
            evidence_url=evidence_url,
            undo=undo,
            cost_usd=cost_usd,
        )

    async def fail(self, step_id: str, error: ErrorClass | str, *, tokens: dict | None = None) -> ActivityLine:
        name = error.name if isinstance(error, ErrorClass) else error
        text = resolve_variant(step_id, status="failed", error_class=name)
        return await self.emit(
            phase=self._phase,
            kind="step",
            status="failed",
            text=interpolate(text, {**self.tokens, **(tokens or {})}),
            step_id=step_id,
        )

    async def search(self, query: str, *, step_id: str = "understand.research_market") -> ActivityLine:
        """Never coalesced — one line per query."""
        await self.flush_coalesce()
        text = resolve_variant("understand.search", status="done")
        return await self.emit(
            phase=self._phase,
            kind="search",
            status="done",
            text=interpolate(text, {**self.tokens, "query": query}),
            step_id=step_id,
            query=query,
        )

    async def code_event(self, verb: str, path: str) -> None:
        """Buffer code events for coalescing."""
        key = ("code", verb)
        now = time.monotonic()
        if self._coalesce_kind and self._coalesce_kind != key:
            await self.flush_coalesce()
        if self._coalesce_kind is None:
            self._coalesce_kind = key
            self._coalesce_started = now
            self._coalesce_buf = []
        self._coalesce_buf.append({"verb": verb, "path": path})
        template_key, min_count = COALESCE_RULES.get(key, (None, 0))
        if template_key is None:
            # emit individually
            for item in self._coalesce_buf:
                await self.emit(
                    phase=self._phase,
                    kind="code",
                    status="done",
                    text="Updated a project file.",
                    step_id="build.landing_page",
                    detail=[item["path"]],
                )
            self._coalesce_buf = []
            self._coalesce_kind = None
            return
        if len(self._coalesce_buf) >= min_count or (now - self._coalesce_started) >= 0.9:
            await self.flush_coalesce()

    async def flush_coalesce(self) -> None:
        if not self._coalesce_buf or not self._coalesce_kind:
            self._coalesce_buf = []
            self._coalesce_kind = None
            return
        kind, verb = self._coalesce_kind
        template_key, min_count = COALESCE_RULES.get((kind, verb), (None, 0))
        paths = [b["path"] for b in self._coalesce_buf]
        count = len(paths)
        if template_key and count >= min_count:
            text = resolve_variant(template_key, status="done")
            await self.emit(
                phase=self._phase,
                kind="code",
                status="done",
                text=interpolate(text, {**self.tokens, "count": count}),
                step_id="build.landing_page",
                detail=paths,
            )
        else:
            for _p in paths:
                await self.emit(
                    phase=self._phase,
                    kind="code",
                    status="done",
                    text="Updated a project file.",
                    step_id="build.landing_page",
                    detail=[_p],
                )
        self._coalesce_buf = []
        self._coalesce_kind = None

    async def _rate_limit(self) -> None:
        now = time.monotonic()
        self._emit_times = [t for t in self._emit_times if now - t < 1.0]
        if len(self._emit_times) >= 10:
            await asyncio_sleep(0.1)
            now = time.monotonic()
            self._emit_times = [t for t in self._emit_times if now - t < 1.0]
        self._emit_times.append(now)

    async def emit(
        self,
        *,
        phase: str,
        kind: str,
        status: str,
        text: str,
        step_id: str | None = None,
        detail: list[str] | None = None,
        query: str | None = None,
        evidence_url: str | None = None,
        undo: dict | None = None,
        cost_usd: float | None = None,
    ) -> ActivityLine:
        await self._rate_limit()
        # Cap text length for safety
        from core.copy import strip_emdash

        text = strip_emdash(text or "").strip()
        if len(text) > 72:
            text = text[:71].rstrip() + "…"
        factory = db()
        # A8 / doc 34: kind=search is a verbatim query line. Progress without a
        # query is a step, not a search.
        if kind == "search" and not (query or "").strip():
            kind = "step"
        line = ActivityLine(
            venture_id=self.venture_id,
            run_id=self.run_id,
            step_id=step_id,
            phase=phase,
            kind=kind,
            status=status,
            text=text,
            detail_json=json.dumps(detail) if detail else None,
            query=query,
            evidence_url=evidence_url,
            undo_json=json.dumps(undo) if undo else None,
            cost_usd=cost_usd,
            ts=utcnow(),
        )
        with factory() as session:
            session.add(line)
            session.commit()
            session.refresh(line)
            session.expunge(line)
        payload = {
            "seq": line.seq,
            "phase": phase,
            "kind": kind,
            "status": status,
            "text": text,
            "query": query,
            "detail": detail,
            "evidence_url": evidence_url,
            "undo": undo,
            "cost_usd": cost_usd,
            "ts": line.ts.isoformat() + "Z" if line.ts else None,
            "step_id": step_id,
            "run_id": self.run_id,
        }
        try:
            await bus.publish("activity", payload, venture_id=self.venture_id, actor="narrator")
        except Exception:
            pass
        return line


def asyncio_sleep(s: float):
    import asyncio

    return asyncio.sleep(s)


# helpers used by unit tests
def coalesce_would_merge(kind: str, verb: str, count: int) -> bool:
    key = COALESCE_RULES.get((kind, verb))
    if not key or key[0] is None:
        return False
    return count >= key[1]

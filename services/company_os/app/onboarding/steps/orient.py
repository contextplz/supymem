"""Phase 0 — orient."""

from __future__ import annotations

from pathlib import Path

from app.intake.normalizer import normalize
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx


def _venture_root(venture_id: str) -> Path:
    # repo root = services/company_os/app/onboarding/steps -> 5 parents up to Orchestrator
    root = Path(__file__).resolve().parents[5]
    return root / "data" / "ventures" / venture_id


async def read_brief(ctx: Ctx) -> StepResult:
    norm = normalize(ctx.venture.raw_idea)
    return StepResult(output={"verbatim": norm["verbatim"], "normalized": norm["normalized"]})


async def create_home(ctx: Ctx) -> StepResult:
    home = _venture_root(ctx.venture.id)
    for sub in ("", "repo", "docs", "mail", "sandbox"):
        (home / sub if sub else home).mkdir(parents=True, exist_ok=True)
    return StepResult(
        output={"home": str(home)},
        tokens={"company": ctx.company},
        detail=[str(home)],
    )


async def save_words(ctx: Ctx) -> StepResult:
    home = _venture_root(ctx.venture.id)
    path = home / "raw_idea.txt"
    path.write_text(ctx.venture.raw_idea, encoding="utf-8")
    # Verify byte-identical
    assert path.read_bytes() == ctx.venture.raw_idea.encode("utf-8")
    return StepResult(output={"path": str(path)})


async def check_permissions(ctx: Ctx) -> StepResult:
    level = ctx.autonomy_word
    return StepResult(
        output={"autonomy": ctx.venture.autonomy_level, "level_word": level},
        tokens={"level": level},
        detail=[f"Autonomy: {level}", "Kill switch: off"],
    )

"""Re-export classify helpers."""

from app.intake.normalizer import classify_deterministic, score_complexity

__all__ = ["classify_deterministic", "score_complexity"]

"""Optional ONNX classifiers. Never downloaded in deterministic_only / tests."""

from __future__ import annotations

import os
from typing import Any

_injection_session: Any = None
_toxicity_session: Any = None


def _model_dir() -> str:
    return (os.getenv("GUARDRAILS_MODEL_DIR") or "").strip()


def score_injection(text: str) -> float | None:
    """Return P(injection) or None when models are not loaded."""
    from guardrails.config import resolve_mode

    if resolve_mode() != "full":
        return None
    session = _load_injection()
    if session is None:
        return None
    return _run(session, text)


def score_toxicity(text: str) -> float | None:
    from guardrails.config import resolve_mode

    if resolve_mode() != "full":
        return None
    session = _load_toxicity()
    if session is None:
        return None
    return _run(session, text)


def _load_injection() -> Any:
    global _injection_session
    if _injection_session is not None:
        return _injection_session
    path = os.path.join(_model_dir(), "prompt_guard.onnx")
    _injection_session = _open_onnx(path)
    return _injection_session


def _load_toxicity() -> Any:
    global _toxicity_session
    if _toxicity_session is not None:
        return _toxicity_session
    path = os.path.join(_model_dir(), "toxic_bert.onnx")
    _toxicity_session = _open_onnx(path)
    return _toxicity_session


def _open_onnx(path: str) -> Any:
    if not path or not os.path.isfile(path):
        return None
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]
    except ImportError:
        return None
    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


def _run(session: Any, text: str) -> float | None:
    # Real tokenization lands with the packaged models in the worker image.
    # Without a tokenizer+model pair we refuse to invent a score.
    _ = (session, text)
    return None

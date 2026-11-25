"""Unit tests for narration copy deck (27 §8 / 34 §3.4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.company_os_harness import load_company_os_app

load_company_os_app()

ROOT = Path(__file__).resolve().parents[2]
DECK_PATH = ROOT / "config" / "narration.yaml"

FORBIDDEN = re.compile(
    r"\b(execute|invoke|API|endpoint|payload|schema|JSON|LLM|prompt|token|agent_spec|"
    r"charter_id|venture_id|sandbox|repo)\b",
    re.I,
)
ID_PREFIX = re.compile(r"\b(ven|chr|obr|as|qst|tsk|msg|doc)_[A-Za-z0-9]+\b")
SNAKE = re.compile(r"\b[a-z]+_[a-z]+\b")
PATHISH = re.compile(r"(/Users/|data/ventures/|\\\\)")


@pytest.fixture(scope="module")
def deck():
    with DECK_PATH.open() as f:
        return yaml.safe_load(f)


def _texts(entry) -> list[str]:
    if isinstance(entry, str):
        return [entry]
    if not isinstance(entry, dict):
        return []
    out = []
    for k, v in entry.items():
        if k in {"note", "detail_hint", "action"}:
            continue
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict) and "text" in v:
            out.append(v["text"])
    return out


def test_deck_has_phase_keys(deck):
    for prefix in (
        "orient.",
        "understand.",
        "validate.",
        "commit.",
        "provision.",
        "build.",
        "handoff.",
    ):
        assert any(k.startswith(prefix) for k in deck if k != "failures"), prefix
    assert "failures" in deck


def test_texts_max_72(deck):
    long_tokens = {
        "company": "Acme Industrial Collective",
        "n": "9",
        "count": "12",
        "level": "Suggest",
        "reason": "three critical unknowns",
        "query": "vintage synth marketplace",
        "demand": "unknown",
        "archetype": "marketplace",
        "audience": "collectors",
        "complexity_word": "straightforward",
        "role_list": "an architect, a builder and a reviewer",
        "url": "http://127.0.0.1:4173",
        "email": "acme@companyos.local",
        "minutes": "12",
        "provider": "Stripe",
        "gate_pass": "11",
        "gate_total": "12",
        "recommendation": "test demand first",
        "stack_word": "a simple web stack",
        "title": "Add user sign-up",
    }
    from app.onboarding.narrator import interpolate

    for key, entry in deck.items():
        if key == "failures":
            for fk, fv in entry.items():
                text = fv.get("text") if isinstance(fv, dict) else fv
                rendered = interpolate(text, long_tokens)
                assert len(rendered) <= 72, (fk, len(rendered), rendered)
            continue
        # Search lines include the verbatim query and may exceed 72 (27 §1 exception).
        if key in {"understand.search", "understand.search_deep"}:
            continue
        for text in _texts(entry):
            rendered = interpolate(text, long_tokens)
            assert len(rendered) <= 72, (key, len(rendered), rendered)


def test_no_forbidden_words(deck):
    from app.onboarding.narrator import interpolate

    for key, entry in deck.items():
        texts = []
        if key == "failures":
            for fv in entry.values():
                if isinstance(fv, dict):
                    texts.append(fv.get("text", ""))
        else:
            texts = _texts(entry)
        for text in texts:
            rendered = interpolate(text, {"company": "Acme", "n": "3", "count": "2", "level": "Suggest",
                                          "reason": "open choices", "query": "widgets", "demand": "mixed",
                                          "archetype": "tool", "audience": "users", "complexity_word": "moderate",
                                          "role_list": "a builder", "url": "http://x.test", "email": "a@b.c",
                                          "provider": "Stripe", "gate_pass": "12", "gate_total": "12",
                                          "recommendation": "go", "stack_word": "web", "title": "Task"})
            assert not FORBIDDEN.search(rendered), (key, rendered)
            assert not ID_PREFIX.search(rendered), (key, rendered)
            assert not PATHISH.search(rendered), (key, rendered)
            for m in SNAKE.finditer(rendered):
                pytest.fail(f"snake_case in {key}: {m.group(0)}")


def test_failures_cover_taxonomy(deck):
    from app.onboarding.failures import ALL_ERROR_CLASSES

    failures = deck["failures"]
    for name, ec in ALL_ERROR_CLASSES.items():
        assert ec.narration_key in failures, (name, ec.narration_key)


def test_simulated_variants(deck):
    for key in ("provision.repo", "provision.database", "provision.email", "provision.hosting"):
        assert "done_simulated" in deck[key], key


def test_no_emoji_or_bang(deck):
    for key, entry in deck.items():
        texts = []
        if key == "failures":
            texts = [fv.get("text", "") for fv in entry.values() if isinstance(fv, dict)]
        else:
            texts = _texts(entry)
        for text in texts:
            assert "!" not in text, (key, text)
            assert not re.search(r"[\U0001F300-\U0001FAFF]", text), (key, text)


def test_interpolate_missing_token_leaves_no_braces():
    from app.onboarding.narrator import interpolate

    assert "{" not in interpolate("Hello {company}.", {})
    assert "{" not in interpolate("Mapped {count} competitors.", {"count": None})
    assert interpolate("At {level}.", {"level": "Suggest"}) == "At Suggest."


def test_registry_keys_exist(deck):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.onboarding.narrator import load_deck
    from app.onboarding.registry import narrate_keys

    # Fragments in narration.d/ are part of the live deck (48 §3).
    full = {**deck, **load_deck(force=True)}
    for key in narrate_keys():
        assert key in full or key.startswith("operate."), key

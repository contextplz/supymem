"""Record/replay cassettes — unit tests must not hit the network."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _digest(messages: list[dict[str, Any]], purpose: str, tier: str) -> str:
    body = json.dumps(
        {"messages": messages, "purpose": purpose, "tier": tier},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass
class Cassette:
    path: Path | None = None
    mode: str = "replay"  # replay | record | passthrough
    store: dict[str, Any] = field(default_factory=dict)

    def load(self) -> None:
        if self.path and self.path.exists():
            self.store = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.store, indent=2), encoding="utf-8")

    def key(self, messages: list[dict[str, Any]], purpose: str, tier: str) -> str:
        return _digest(messages, purpose, tier)

    def get(self, key: str) -> dict[str, Any] | None:
        return self.store.get(key)

    def put(self, key: str, response: dict[str, Any]) -> None:
        self.store[key] = response
        if self.mode == "record":
            self.save()


# In-memory deterministic fixtures for tests / offline mode.
DETERMINISTIC_FIXTURES: dict[str, dict[str, Any]] = {
    "idea.board": {
        "content": json.dumps({
            "score": 5,
            "vote": "Back if",
            "risks": [
                {"area": "demand", "risk": "You have not yet shown that your customer needs this outcome urgently."},
                {"area": "distribution", "risk": "You have not yet shown how you can reach willing customers."},
                {"area": "delivery", "risk": "You have not yet demonstrated a reliable first customer outcome."},
            ],
            "questions": [
                "What does your customer do today, and what makes that difficult?",
                "Where can you ask consenting customers about that workflow?",
                "What is the smallest outcome you can demonstrate for one customer?",
            ],
            "conditions": ["You demonstrate the smallest useful outcome and test your assumptions with consenting customers."],
        }),
        "tokens_in": 0,
        "tokens_out": 0,
    },
    "intake.classify": {
        "content": json.dumps(
            {
                "domain": "food_service",
                "business_model": "saas",
                "build_archetype": "crud_web",
                "audience": "home bakers",
                "monetization": "subscription",
                "regulatory_surface": "none",
                "novelty": "differentiated",
            }
        ),
        "tokens_in": 100,
        "tokens_out": 80,
    },
    "charter.synthesize": {
        "content": json.dumps(
            {
                "one_liner": "Custom cake orders on WhatsApp for home bakers",
                "problem": "Home bakers lose orders in chat chaos",
                "audience": "home bakers",
                "jobs_to_be_done": ["take orders", "confirm details", "collect payment"],
                "mvp_scope": ["WhatsApp intake", "order board", "basic payments"],
                "explicit_non_goals": ["native iOS app"],
                "business_model": "saas",
                "pricing_hypothesis": "$19/mo",
                "success_metric": "orders_per_week",
                "success_threshold": 10,
                "build_archetype": "crud_web",
                "integrations": ["whatsapp", "stripe"],
                "regulatory_notes": [],
                "risks": [],
                "gtm_order": "market_first",
                "channels": ["landing", "x"],
                "brand": {"name": "BakeOrder", "tone": "warm"},
                "open_questions": [],
            }
        ),
        "tokens_in": 200,
        "tokens_out": 400,
    },
    "clarify.generate_questions": {
        "content": json.dumps({"questions": []}),
        "tokens_in": 150,
        "tokens_out": 50,
    },
    "gtm.god_diagnose": {
        "content": json.dumps({"now": []}),
        "tokens_in": 80,
        "tokens_out": 40,
    },
    "gtm.god_research": {
        "content": json.dumps({}),
        "tokens_in": 80,
        "tokens_out": 40,
    },
}


def fixture_for(purpose: str) -> dict[str, Any] | None:
    return DETERMINISTIC_FIXTURES.get(purpose)

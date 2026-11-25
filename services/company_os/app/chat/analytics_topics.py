"""Coarse, private-by-construction topics for operator demand analytics.

These labels are descriptive demand signals, not promises that work executed.
No founder-entered token or entity is copied into the event stream.
"""
from __future__ import annotations

import re

_TOPICS = {
    "account_onboarding": r"\b(?:sign[ -]?in|log[ -]?in|onboard|invite|account|reconnect)\b",
    "agents_team": r"\b(?:agent|team|hire|researcher|builder|concierge)\b",
    "analytics_measurement": r"\b(?:analytics|metric|dashboard|retention|active users?|usage data|tracking)\b",
    "case_studies": r"\b(?:case stud(?:y|ies)|playbook|precedent|reference company)\b",
    "computer_use": r"\b(?:computer use|browser task|mac task|cursor|codex|terminal|desktop control)\b",
    "credits_billing": r"\b(?:credits?|billing|payment|invoice|subscription|costs?)\b",
    "gtm_acquisition": r"\b(?:gtm|go[ -]?to[ -]?market|outreach|leads?|customers?|marketing|campaign)\b",
    "integrations": r"\b(?:integration|connect(?:ion)?s?|composio|gmail|api key|oauth)\b",
    "market_research": r"\b(?:market|competitor|research|demand|icp|audience)\b",
    "planning": r"\b(?:plan|milestone|roadmap|priority|next step)\b",
    "product_build": r"\b(?:product|app(?:lication)?|build|builder|code|source|qa|deploy|hosted|launch)\b",
    "status_blocker": r"\b(?:status|blocked|blocker|stuck|progress|working|done|doing now|right now|what(?:'s| is) next)\b",
    "website_waitlist": r"\b(?:website|landing page|waitlist|signups?|public site|conversion)\b",
}
_COMPILED = {name: re.compile(pattern, re.I) for name, pattern in _TOPICS.items()}


def classify_topics(message: str) -> list[str]:
    if not isinstance(message, str):
        return ["other"]
    found = sorted(name for name, pattern in _COMPILED.items() if pattern.search(message[:8000]))
    return found[:4] or ["other"]

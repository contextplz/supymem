"""Validate catalog directions invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[4]
CATALOG = REPO / "config" / "concierge_catalog"


def load_all() -> list[dict[str, Any]]:
    out = []
    for p in sorted(CATALOG.glob("*.yaml")):
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            data["_file"] = p.name
            out.append(data)
    return out


def validate_entry(entry: dict[str, Any]) -> list[str]:
    errs = []
    for i, step in enumerate(entry.get("steps") or []):
        url = step.get("url") or ""
        if not url or url.rstrip("/") in {
            "https://stripe.com",
            "https://github.com",
            "https://vercel.com",
            "https://openai.com",
        }:
            # homepage-only rejected
            if url.count("/") <= 3 and "?" not in url and url.rstrip("/").endswith(
                (".com", ".io", ".dev")
            ):
                errs.append(f"step{i}: homepage_not_deep_link:{url}")
        if not step.get("ui_locator"):
            errs.append(f"step{i}: missing ui_locator")
        if not step.get("expect_to_see"):
            errs.append(f"step{i}: missing expect_to_see")
    return errs

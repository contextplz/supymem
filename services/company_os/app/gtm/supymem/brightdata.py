"""Bright Data LinkedIn *research* client (profile + posts). Never sends DMs."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

PROFILE_DATASET_ID = "gd_l1viktl72bvl7bjuj0"
POSTS_DATASET_ID = "gd_lyy3tktm25m4avu764"
BRIGHTDATA_BASE = "https://api.brightdata.com/datasets/v3/scrape"


def configured() -> bool:
    return bool(os.environ.get("BRIGHTDATA_API_KEY", "").strip())


def _key() -> str:
    key = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY is not configured.")
    return key


def _timeout_ms(name: str, fallback: int) -> float:
    raw = os.environ.get(name)
    try:
        parsed = int(raw) if raw else fallback
    except ValueError:
        parsed = fallback
    return max(5_000, min(parsed, 300_000)) / 1000.0


def _first_str(*vals: Any) -> str | None:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return None


def _parse_payload(text: str) -> Any:
    trimmed = text.strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        records = []
        for line in trimmed.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                return None
        return records or None


def _unwrap_record(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and not item.get("error"):
                return item
        return None
    if isinstance(payload, dict):
        for key in ("data", "result", "results"):
            if isinstance(payload.get(key), list):
                return _unwrap_record(payload[key])
        return payload
    return None


def normalize_linkedin_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    import re

    m = re.search(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", url.strip(), re.I)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}/"
    return None


def normalize_profile(payload: Any) -> dict[str, Any]:
    rec = _unwrap_record(payload) or {}
    out: dict[str, Any] = {}
    name = _first_str(rec.get("full_name"), rec.get("name"), rec.get("display_name"))
    if name:
        out["name"] = name
    headline = _first_str(rec.get("headline"))
    if headline:
        out["headline"] = headline
    about = _first_str(rec.get("about"), rec.get("summary"), rec.get("bio"))
    if about:
        out["about"] = about
    company = _first_str(
        rec.get("company"), rec.get("current_company_name"), rec.get("company_name")
    )
    current = rec.get("current_company")
    if not company and isinstance(current, dict):
        company = _first_str(current.get("name"), current.get("company_name"))
    if company:
        out["company"] = company
    position = _first_str(
        rec.get("position"), rec.get("title"), rec.get("current_title")
    )
    if position:
        out["position"] = position
    location = _first_str(rec.get("location"), rec.get("city"), rec.get("country"))
    if location:
        out["location"] = location
    url = _first_str(rec.get("url"), rec.get("linkedin_url"), rec.get("profile_url"))
    if url:
        out["linkedin_url"] = url
    return out


def _scrape(dataset_qs: str, body: dict[str, Any], *, timeout: float) -> Any:
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }
    url = f"{BRIGHTDATA_BASE}?{dataset_qs}"
    with httpx.Client(timeout=timeout) as client:
        res = client.post(url, headers=headers, json=body)
    text = res.text
    if res.status_code == 402 or "credits exhausted" in text.lower():
        raise RuntimeError("Bright Data credits exhausted.")
    if res.status_code >= 400:
        raise RuntimeError(f"Bright Data returned HTTP {res.status_code}.")
    parsed = _parse_payload(text)
    if parsed is None:
        raise RuntimeError("Bright Data returned a non-JSON response.")
    return parsed


def scrape_posts(linkedin_url: str) -> dict[str, Any]:
    url = normalize_linkedin_url(linkedin_url) or linkedin_url
    try:
        raw = _scrape(
            f"dataset_id={POSTS_DATASET_ID}&format=json",
            {"input": [{"url": url}]},
            timeout=_timeout_ms("BRIGHTDATA_POSTS_TIMEOUT_MS", 90_000),
        )
        recs = raw if isinstance(raw, list) else [raw]
        lines: list[str] = []
        for rec in recs[:8]:
            if not isinstance(rec, dict):
                continue
            text = _first_str(rec.get("post_text"), rec.get("text"), rec.get("description"))
            when = _first_str(rec.get("date"), rec.get("posted_at"), rec.get("created_at"))
            if text:
                lines.append(f"- ({when or 'undated'}) {text[:280]}")
        notes = ""
        if lines:
            notes = "LINKEDIN POSTS (CONFIRMED):\n" + "\n".join(lines)
        return {"ok": bool(lines), "notes": notes, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.info("brightdata posts failed: %s", exc)
        return {"ok": False, "notes": "", "error": str(exc)}


def scrape_profile(linkedin_url: str) -> dict[str, Any]:
    url = normalize_linkedin_url(linkedin_url) or linkedin_url
    try:
        raw = _scrape(
            f"dataset_id={PROFILE_DATASET_ID}&format=json",
            {"input": [{"url": url}]},
            timeout=_timeout_ms("BRIGHTDATA_PROFILE_TIMEOUT_MS", 90_000),
        )
        profile = normalize_profile(raw)
        return {"ok": bool(profile), "profile": profile, "error": None if profile else "empty_dataset"}
    except Exception as exc:  # noqa: BLE001
        log.info("brightdata profile failed: %s", exc)
        return {"ok": False, "profile": {}, "error": str(exc)}


def format_research_notes(profile: dict[str, Any]) -> str:
    lines = [
        "SOURCE: LinkedIn profile via Bright Data (CONFIRMED, not inference).",
        "",
        "PERSON:",
    ]
    for label, key in (
        ("Name", "name"),
        ("Headline", "headline"),
        ("Current position", "position"),
        ("Company", "company"),
        ("Location", "location"),
    ):
        if profile.get(key):
            lines.append(f"- {label}: {profile[key]}")
    if profile.get("about"):
        about = str(profile["about"]).replace("\n", " ")[:800]
        lines.append(f"- About: {about}")
    if len(lines) <= 3:
        lines.append("- (no profile fields returned)")
    lines.append("")
    lines.append(
        "PROVENANCE: treat PERSON as CONFIRMED FACTS. Never invent a post or milestone."
    )
    return "\n".join(lines)

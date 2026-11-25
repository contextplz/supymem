"""The uploaded JSON brief: product, ICP, competitors, voice, claims, cadence.

Everything the content engine says has to trace back to this file. The claims
list in particular is a registry — a post may not assert a number or a product
capability that does not appear here, which is what stops the generator from
inventing proof.
"""

from __future__ import annotations

import json
from typing import Any

from core.clock import as_utc, utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import ContentBrief
from app.db.session import serialize_writes
from app.state import db

# Blank intake only: example metrics, prices and evidence must never enter a
# founder company as if supplied or verified. Required fields need founder input.
TEMPLATE: dict[str, Any] = {
    "product": {
        "name": "",
        "one_liner": "",
        "category": "",
        "url": "",
        "differentiators": []
    },
    "icp": {
        "segments": [
            {
                "name": "",
                "role": "",
                "company_size": "",
                "industry": "",
                "geography": "",
                "pains": [],
                "triggers": [],
                "objections": [],
                "watering_holes": []
            }
        ]
    },
    "competitors": [],
    "voice": {
        "tone": "direct, concrete, no hype",
        "person": "first",
        "reading_level": "plain",
        "banned_phrases": []
    },
    "claims": [],
    "cadence": {
        "value_to_promo": 4,
        "linkedin": {
            "per_week": 0
        },
        "x": {
            "per_day": 0
        },
        "reddit": {
            "per_week": 0
        }
    }
}


class BriefError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _as_str_list(value: Any) -> list[str]:
    return [str(v).strip() for v in _as_list(value) if str(v).strip()]


def validate(data: Any) -> dict[str, Any]:
    """Normalize an uploaded brief or raise BriefError with every problem."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise BriefError(["The brief must be a JSON object."])

    product_raw = data.get("product")
    product: dict[str, Any] = product_raw if isinstance(product_raw, dict) else {}
    name = str(product.get("name") or "").strip()
    one_liner = str(product.get("one_liner") or "").strip()
    if not name:
        errors.append("product.name is required.")
    if not one_liner:
        errors.append("product.one_liner is required.")

    icp_raw = data.get("icp")
    icp: dict[str, Any] = icp_raw if isinstance(icp_raw, dict) else {}
    segments_raw = _as_list(icp.get("segments"))
    segments: list[dict[str, Any]] = []
    for i, seg in enumerate(segments_raw):
        if not isinstance(seg, dict):
            errors.append(f"icp.segments[{i}] must be an object.")
            continue
        seg_name = str(seg.get("name") or "").strip()
        if not seg_name:
            errors.append(f"icp.segments[{i}].name is required.")
            continue
        segments.append(
            {
                "name": seg_name,
                "role": str(seg.get("role") or "").strip(),
                "company_size": str(seg.get("company_size") or "").strip(),
                "industry": str(seg.get("industry") or "").strip(),
                "geography": str(seg.get("geography") or "").strip(),
                "pains": _as_str_list(seg.get("pains")),
                "triggers": _as_str_list(seg.get("triggers")),
                "objections": _as_str_list(seg.get("objections")),
                "watering_holes": _as_str_list(seg.get("watering_holes")),
            }
        )
    if not segments:
        errors.append("icp.segments needs at least one segment with a name.")

    competitors: list[dict[str, Any]] = []
    for i, comp in enumerate(_as_list(data.get("competitors"))):
        if not isinstance(comp, dict):
            errors.append(f"competitors[{i}] must be an object.")
            continue
        comp_name = str(comp.get("name") or "").strip()
        if not comp_name:
            errors.append(f"competitors[{i}].name is required.")
            continue
        competitors.append(
            {
                "name": comp_name,
                "url": str(comp.get("url") or "").strip(),
                "positioning": str(comp.get("positioning") or "").strip(),
                "strengths": _as_str_list(comp.get("strengths")),
                "weaknesses": _as_str_list(comp.get("weaknesses")),
                "pricing": str(comp.get("pricing") or "").strip(),
            }
        )

    voice_raw = data.get("voice")
    voice_in: dict[str, Any] = voice_raw if isinstance(voice_raw, dict) else {}
    voice = {
        "tone": str(voice_in.get("tone") or "direct, concrete, no hype").strip(),
        "person": str(voice_in.get("person") or "first").strip(),
        "reading_level": str(voice_in.get("reading_level") or "plain").strip(),
        "banned_phrases": _as_str_list(voice_in.get("banned_phrases")),
    }

    claims: list[dict[str, str]] = []
    for i, claim in enumerate(_as_list(data.get("claims"))):
        if isinstance(claim, str):
            claims.append({"text": claim.strip(), "evidence": "", "source": ""})
            continue
        if not isinstance(claim, dict):
            errors.append(f"claims[{i}] must be a string or an object.")
            continue
        text = str(claim.get("text") or "").strip()
        if not text:
            errors.append(f"claims[{i}].text is required.")
            continue
        claims.append(
            {
                "text": text,
                "evidence": str(claim.get("evidence") or "").strip(),
                "source": str(claim.get("source") or "").strip(),
            }
        )

    cadence_raw = data.get("cadence")
    cadence_in: dict[str, Any] = cadence_raw if isinstance(cadence_raw, dict) else {}
    try:
        ratio = int(cadence_in.get("value_to_promo") or 4)
    except (TypeError, ValueError):
        ratio = 4
        errors.append("cadence.value_to_promo must be a whole number.")
    cadence: dict[str, Any] = {"value_to_promo": max(1, ratio)}
    for platform in ("linkedin", "x", "reddit"):
        entry = cadence_in.get(platform)
        if isinstance(entry, dict):
            cadence[platform] = {
                k: int(v)
                for k, v in entry.items()
                if k in {"per_day", "per_week"} and isinstance(v, (int, float))
            }

    if errors:
        raise BriefError(errors)

    return {
        "product": {
            "name": name,
            "one_liner": one_liner,
            "category": str(product.get("category") or "").strip(),
            "url": str(product.get("url") or "").strip(),
            "differentiators": _as_str_list(product.get("differentiators")),
            "proof": _as_str_list(product.get("proof")),
        },
        "icp": {"segments": segments},
        "competitors": competitors,
        "voice": voice,
        "claims": claims,
        "cadence": cadence,
    }


def save(
    *, workspace_id: str, data: Any, source_name: str | None = None,
    venture_id: str | None = None,
) -> dict[str, Any]:
    """Validate and store as a new active version; the previous one is archived."""
    normalized = validate(data)
    now = utcnow()
    factory = db()
    with factory() as session:
        serialize_writes(
            session, key=f"content-brief:{workspace_id}:{venture_id or 'workspace'}"
        )
        previous = session.scalars(
            select(ContentBrief)
            .where(ContentBrief.workspace_id == workspace_id,
                   ContentBrief.venture_id == venture_id)
            .order_by(ContentBrief.version.desc())
        ).all()
        version = (previous[0].version + 1) if previous else 1
        for row in previous:
            row.status = "archived"
        fresh = ContentBrief(
            id=new_id("cbr"),
            workspace_id=workspace_id,
            venture_id=venture_id,
            version=version,
            status="active",
            source_name=source_name,
            product_json=json.dumps(normalized["product"]),
            icp_json=json.dumps(normalized["icp"]),
            competitors_json=json.dumps(normalized["competitors"]),
            voice_json=json.dumps(normalized["voice"]),
            claims_json=json.dumps(normalized["claims"]),
            cadence_json=json.dumps(normalized["cadence"]),
            created_at=now,
        )
        session.add(fresh)
        session.commit()
        session.refresh(fresh)
        return view(fresh)


def active(workspace_id: str, venture_id: str | None = None) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ContentBrief)
            .where(
                ContentBrief.workspace_id == workspace_id,
                ContentBrief.venture_id == venture_id,
                ContentBrief.status == "active",
            )
            .order_by(ContentBrief.version.desc())
        ).first()
        return view(row) if row else None


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def view(row: ContentBrief) -> dict[str, Any]:
    return {
        "id": row.id,
        "venture_id": row.venture_id,
        "version": row.version,
        "status": row.status,
        "source_name": row.source_name,
        "product": _loads(row.product_json, {}),
        "icp": _loads(row.icp_json, {}),
        "competitors": _loads(row.competitors_json, []),
        "voice": _loads(row.voice_json, {}),
        "claims": _loads(row.claims_json, []),
        "cadence": _loads(row.cadence_json, {}),
        "created_at": as_utc(row.created_at).isoformat(),
    }

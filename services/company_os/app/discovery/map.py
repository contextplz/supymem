"""Company Map persistence (38 §3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.audit.writer import audit
from app.db.models import CompanyMap
from app.discovery.housestyle import extract_house_style
from app.discovery.scan import scan_workspace
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow
from core.ids import new_id


@dataclass(frozen=True)
class MapView:
    id: str
    workspace_id: str
    version: int
    content: dict[str, Any]
    confidence: dict[str, Any]
    house_style: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "version": self.version,
            "content": self.content,
            "confidence": self.confidence,
            "house_style": self.house_style,
            "created_at": self.created_at,
        }


def _view(row: CompanyMap) -> MapView:
    return MapView(
        id=row.id,
        workspace_id=row.workspace_id,
        version=row.version,
        content=_json(row.content_json, {}),
        confidence=_json(row.confidence_json, {}),
        house_style=_json(row.house_style_json, {}),
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or json.dumps(default))
    except json.JSONDecodeError:
        return default


def latest_map(workspace_id: str) -> MapView | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(CompanyMap)
            .where(CompanyMap.workspace_id == workspace_id)
            .order_by(CompanyMap.version.desc())
        ).first()
        return _view(row) if row else None


def write_map(
    workspace_id: str,
    content: dict[str, Any],
    *,
    confidence: dict[str, Any] | None = None,
    house_style: dict[str, Any] | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> MapView:
    factory = db()
    with factory() as session:
        current = session.scalar(
            select(func.max(CompanyMap.version)).where(
                CompanyMap.workspace_id == workspace_id
            )
        )
        version = int(current or 0) + 1
        row = CompanyMap(
            id=new_id("cmp"),
            workspace_id=workspace_id,
            version=version,
            content_json=json.dumps(content),
            confidence_json=json.dumps(confidence or {}),
            corrections_json="[]",
            house_style_json=json.dumps(house_style or {}),
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        view = _view(row)

    audit(
        "company.map.written",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"version": view.version},
    )
    return view


async def discover_and_map(
    workspace_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> MapView:
    """Scan connections, derive house style, persist a Company Map artifact."""
    inventory = await scan_workspace(workspace_id, actor_id=actor_id)
    files: list[dict[str, Any]] = []
    surfaces: list[str] = []
    for con in inventory.get("connections") or []:
        files.extend(con.get("files") or [])
        for res in con.get("resources") or []:
            surfaces.append(str(res.get("ref") or res.get("display") or ""))

    style = extract_house_style(files)
    content = {
        "product_surfaces": {
            "items": surfaces,
            "provenance": "observed",
        },
        "stack_and_conventions": {
            "hints": _stack_hints(files),
            "provenance": "observed",
        },
        "house_style": style,
        "delivery_rhythm": {"provenance": "inferred", "note": "not enough history yet"},
        "who_does_what": {"provenance": "inferred", "note": "handles only; unconfirmed"},
        "pain_signals": {"items": [], "provenance": "observed"},
        "guardrails": {
            "observe_floor": "no merge/deploy/delete without human approval",
            "provenance": "you_told_me",
        },
        "inventory": inventory,
    }
    confidence = {
        "product_surfaces": 0.7 if surfaces else 0.3,
        "stack_and_conventions": 0.65 if files else 0.4,
        "house_style": float(style.get("confidence") or 0.5),
        "delivery_rhythm": 0.2,
        "who_does_what": 0.2,
        "pain_signals": 0.3,
        "guardrails": 1.0,
    }
    # Sections under 0.6 stay as questions in the API response.
    for section, score in list(confidence.items()):
        if score < 0.6 and section in content:
            block = content[section]
            if isinstance(block, dict):
                block["as_question"] = True
                block["question"] = f"Is this right for {section.replace('_', ' ')}?"

    return write_map(
        workspace_id,
        content,
        confidence=confidence,
        house_style=style,
        actor_id=actor_id,
        surface=surface,
    )


def _stack_hints(files: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for f in files:
        path = str(f.get("path") or "").lower()
        body = str(f.get("content") or "").lower()
        if path.endswith("pyproject.toml") or "fastapi" in body:
            hints.append("python")
        if "fastapi" in body:
            hints.append("fastapi")
        if path.endswith("package.json") or '"react"' in body:
            hints.append("javascript")
        if "pytest" in body:
            hints.append("pytest")
    return sorted(set(hints))

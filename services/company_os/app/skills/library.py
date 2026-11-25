"""Global SQLite-backed skill library."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


def _default_db_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "skill_library.sqlite3"


@dataclass(frozen=True, slots=True)
class SkillRecord:
    id: str
    pack_id: str
    pack_ref: str
    name: str
    slug: str
    category: str
    description: str
    triggers: tuple[str, ...]
    frameworks: tuple[str, ...]
    tags: tuple[str, ...]
    body_path: str
    body_sha256: str
    body_tokens: int
    has_scripts: bool
    has_templates: bool
    has_references: bool
    license: str
    redistribute: bool
    tier: str = "extended"
    quality_score: float = 0.0
    reject_reason: str | None = None


class SkillLibrary:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        from app.db.session import _apply_sqlite_pragmas

        connection = sqlite3.connect(self.path)
        try:
            _apply_sqlite_pragmas(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skill_records (
                slug TEXT PRIMARY KEY, record_json TEXT NOT NULL
                )"""
            )

    def upsert(self, record: SkillRecord) -> None:
        payload = asdict(record)
        for key in ("triggers", "frameworks", "tags"):
            payload[key] = list(payload[key])
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO skill_records(slug, record_json) VALUES (?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET record_json=excluded.record_json",
                (record.slug, json.dumps(payload, sort_keys=True)),
            )

    def all(self) -> list[SkillRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT record_json FROM skill_records").fetchall()
        return [self._decode(row[0]) for row in rows]

    def get(self, slug: str) -> SkillRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT record_json FROM skill_records WHERE slug=?", (slug,)).fetchone()
        return self._decode(row[0]) if row else None

    @staticmethod
    def _decode(payload: str) -> SkillRecord:
        data = json.loads(payload)
        for key in ("triggers", "frameworks", "tags"):
            data[key] = tuple(data[key])
        return SkillRecord(**data)

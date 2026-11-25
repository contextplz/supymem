"""The Phase 10 backfill must not lose a row (46 T10.2, B27, invariant I1).

Seeds a realistic pre-Phase-10 database at the previous revision, upgrades, and
checks that every venture and every child row is reachable through a workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "a1b2c3d4e5f6"
PHASE10_REVISION = "c7d1e2f3a4b5"

VENTURES = [
    ("ven_aaaaaaaaaaaa", "Vintage Synths", "a marketplace for vintage synths", "L2"),
    ("ven_bbbbbbbbbbbb", "Dog Walkers", "an app for booking dog walkers", "L1"),
    ("ven_cccccccccccc", "Tea Club", "a subscription for rare teas", "L3"),
]


def _cfg(db_path: Path) -> Config:
    ini = ROOT / "services" / "company_os" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option(
        "script_location", str(ROOT / "services/company_os/app/db/migrations")
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{db_path}"
    cos = str(ROOT / "services" / "company_os")
    if cos not in sys.path:
        sys.path.insert(0, cos)
    return cfg


def _seed(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for vid, name, idea, autonomy in VENTURES:
            conn.execute(
                text(
                    "INSERT INTO ventures (id, name, raw_idea, stage, autonomy_level,"
                    " created_at, updated_at) VALUES"
                    " (:id, :name, :idea, 'draft', :autonomy,"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": vid, "name": name, "idea": idea, "autonomy": autonomy},
            )
            conn.execute(
                text(
                    "INSERT INTO onboarding_runs (id, venture_id, mode, status, phase,"
                    " phases_done, phases_total, flags_json, research_founder,"
                    " started_at, updated_at) VALUES"
                    " (:id, :vid, 'guided', 'done', 'handoff', 7, 7, '{}', 0,"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": f"obr{vid[3:]}", "vid": vid},
            )
            for n in range(3):
                conn.execute(
                    text(
                        "INSERT INTO activity_lines (venture_id, phase, kind, status,"
                        " text, ts) VALUES (:vid, 'orient', 'step', 'done',"
                        " 'Did a thing.', CURRENT_TIMESTAMP)"
                    ),
                    {"vid": vid},
                )
                conn.execute(
                    text(
                        "INSERT INTO chat_messages (id, venture_id, role, kind, body_md,"
                        " created_at) VALUES (:id, :vid, 'assistant', 'text', 'hi',"
                        " CURRENT_TIMESTAMP)"
                    ),
                    {"id": f"cmg_{vid[4:10]}{n}", "vid": vid},
                )
            conn.execute(
                text(
                    "INSERT INTO planned_tasks (id, venture_id, title, why_now,"
                    " tags_json, est_minutes, runnable, source, status, rank,"
                    " created_at, updated_at) VALUES (:id, :vid, 'Do it',"
                    " 'because', '[]', 30, 1, 'planner', 'open', 0,"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": f"tsk{vid[3:]}", "vid": vid},
            )
            conn.execute(
                text(
                    "INSERT INTO venture_resources (id, venture_id, resource, provider,"
                    " simulated, handle, display, created_at, updated_at) VALUES"
                    " (:id, :vid, 'repo', 'local', 1, 'h', 'Repo',"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": f"vrs{vid[3:]}", "vid": vid},
            )
    engine.dispose()


@pytest.fixture()
def upgraded(tmp_path: Path) -> Path:
    db_path = tmp_path / "backfill.db"
    old = os.getcwd()
    os.chdir(ROOT / "services" / "company_os")
    try:
        cfg = _cfg(db_path)
        command.upgrade(cfg, PREVIOUS_REVISION)
        _seed(db_path)
        command.upgrade(cfg, PHASE10_REVISION)
    finally:
        os.chdir(old)
    return db_path


def test_every_venture_gets_a_workspace(upgraded: Path) -> None:
    engine = create_engine(f"sqlite:///{upgraded}")
    with engine.begin() as conn:
        ventures = conn.execute(text("SELECT COUNT(*) FROM ventures")).scalar_one()
        workspaces = conn.execute(
            text("SELECT COUNT(*) FROM workspaces WHERE kind='venture'")
        ).scalar_one()
        assert ventures == len(VENTURES)
        assert workspaces == ventures

        unlinked = conn.execute(
            text("SELECT COUNT(*) FROM ventures WHERE workspace_id IS NULL")
        ).scalar_one()
        assert unlinked == 0

        # Autonomy carries over rather than resetting to the default.
        level = conn.execute(
            text(
                "SELECT w.autonomy_level FROM workspaces w JOIN ventures v"
                " ON v.workspace_id = w.id WHERE v.id = 'ven_cccccccccccc'"
            )
        ).scalar_one()
        assert level == "L3"
    engine.dispose()


def test_child_rows_are_reachable_through_the_workspace(upgraded: Path) -> None:
    engine = create_engine(f"sqlite:///{upgraded}")
    with engine.begin() as conn:
        for table, expected in (
            ("activity_lines", 3 * len(VENTURES)),
            ("chat_messages", 3 * len(VENTURES)),
            ("onboarding_runs", len(VENTURES)),
            ("planned_tasks", len(VENTURES)),
            ("venture_resources", len(VENTURES)),
        ):
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            linked = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NOT NULL")
            ).scalar_one()
            assert total == expected, table
            assert linked == expected, f"{table}: {total - linked} rows unlinked"
    engine.dispose()


def test_desk_and_owner_exist_after_backfill(upgraded: Path) -> None:
    engine = create_engine(f"sqlite:///{upgraded}")
    with engine.begin() as conn:
        desks = conn.execute(
            text("SELECT COUNT(*) FROM workspaces WHERE kind='desk'")
        ).scalar_one()
        assert desks == 1
        owners = conn.execute(
            text("SELECT COUNT(*) FROM workspace_members WHERE role='owner'")
        ).scalar_one()
        assert owners == len(VENTURES) + 1
    engine.dispose()


def test_activity_lines_remain_append_only_after_backfill(upgraded: Path) -> None:
    """The guard triggers must be back on after the structural update."""
    engine = create_engine(f"sqlite:///{upgraded}")
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("UPDATE activity_lines SET text = 'tampered'"))
    engine.dispose()
    engine2 = create_engine(f"sqlite:///{upgraded}")
    with engine2.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM activity_lines"))
    engine2.dispose()

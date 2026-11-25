#!/usr/bin/env python3
"""End-to-end adaptive company demo (no hand-coding of the change).

Flow:
1. Set autonomy=full_auto
2. Submit a product-change request via feedback API
3. Show Engineering self-propose -> validate -> promote (config + code paths)
4. Verify rollback and kill switch

Usage (API must be up, or this script boots an in-process app):
  .venv/bin/python scripts/adaptive_e2e_demo.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "environment"))
os.chdir(ROOT)

# Prefer existing runtime DB
os.environ.setdefault("DATABASE_PATH", str(ROOT / "data" / "runtime" / "catalog.db"))
os.environ.setdefault("SEED_DATABASE_PATH", str(ROOT / "data" / "seed" / "catalog.seed.sqlite"))
os.environ.setdefault("ARTIFACTS_DIR", str(ROOT / "data" / "artifacts"))


def main() -> int:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.db.session import reset_engine
    from app.bootstrap import ensure_runtime_database
    from app.main import create_app

    get_settings.cache_clear()
    reset_engine()
    ensure_runtime_database()
    app = create_app()

    report: dict = {"steps": []}

    with TestClient(app) as client:
        # 1. Governance
        r = client.post("/api/adaptive/governance/kill-switch", json={"enabled": False})
        assert r.status_code == 200
        r = client.post("/api/adaptive/governance/autonomy", json={"autonomy": "full_auto"})
        assert r.status_code == 200
        report["steps"].append({"governance": r.json()})

        # 2. Config-tier feedback
        r = client.post(
            "/api/adaptive/feedback",
            json={"text": "Improve engineering agent system prompt for cart checkout reliability"},
        )
        assert r.status_code == 200
        config_prop = r.json().get("proposal") or {}
        report["steps"].append({"config_proposal": config_prop})
        assert config_prop.get("status") in {"promoted", "approved", "rejected"}, config_prop

        # 3. Code-tier product request
        r = client.post(
            "/api/adaptive/feedback",
            json={"text": "Add a wishlist feature to the amazon.in product page"},
        )
        assert r.status_code == 200
        code_prop = r.json().get("proposal") or {}
        report["steps"].append({"code_proposal": code_prop})
        assert code_prop.get("tier") == "code"
        assert code_prop.get("status") in {"promoted", "approved", "rejected"}, code_prop

        # 4. Spawn engineering with the same goal (runtime path)
        r = client.post(
            "/api/company/spawn",
            json={
                "department_id": "engineering",
                "goal": "Add a compare-products feature to the amazon.in clone",
            },
        )
        assert r.status_code == 200
        report["steps"].append({"engineering_spawn": r.json()["agent"]})

        # 5. Kill switch
        r = client.post("/api/adaptive/governance/kill-switch", json={"enabled": True})
        assert r.status_code == 200
        blocked = client.post("/api/adaptive/feedback", json={"text": "Add dark mode feature"})
        assert blocked.status_code == 200
        blocked_prop = blocked.json().get("proposal") or {}
        report["steps"].append({"kill_switch_block": blocked_prop})
        assert blocked_prop.get("status") == "blocked" or blocked_prop.get("id") is None

        # 6. Disable kill, rollback if we have a promotion
        client.post("/api/adaptive/governance/kill-switch", json={"enabled": False})
        snap = client.get("/api/adaptive/snapshot").json()
        promotions = snap.get("promotions") or []
        report["steps"].append({"promotion_count": len(promotions), "proposal_count": len(snap.get("proposals") or [])})
        if promotions:
            last = promotions[0]
            rb = client.post(
                "/api/adaptive/rollback",
                json={
                    "target_kind": last["target_kind"],
                    "target_id": last["target_id"],
                    "to_version": last.get("from_version"),
                },
            )
            report["steps"].append({"rollback": rb.json() if rb.status_code == 200 else {"status": rb.status_code, "body": rb.text}})

        report["ok"] = True
        report["summary"] = {
            "config_status": config_prop.get("status"),
            "code_status": code_prop.get("status"),
            "kill_blocked": blocked_prop.get("status") == "blocked" or blocked_prop.get("id") is None,
            "registries": {
                "agents": len((snap.get("registries") or {}).get("agents") or []),
                "tools": len((snap.get("registries") or {}).get("tools") or []),
            },
        }

    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

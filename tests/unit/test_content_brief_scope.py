from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_active_content_brief_is_isolated_by_venture(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/content-scope.db")
    from app.db.models import Base, Venture
    from app.state import db, reset_db_factory
    from app.content import brief
    from core.clock import utcnow

    reset_db_factory()
    factory = db()
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
        now = utcnow()
        for venture_id, name in (("ven_first", "First"), ("ven_second", "Second")):
            session.add(Venture(
                id=venture_id, workspace_id="wsp_one", name=name, raw_idea=name,
                stage="draft", autonomy_level="L1", created_at=now, updated_at=now,
            ))
        session.commit()

    def payload(name: str) -> dict:
        return {
            "product": {"name": name, "one_liner": f"{name} description"},
            "icp": {"segments": [{"name": f"{name} buyers", "pains": ["A real pain"]}]},
            "competitors": [],
            "voice": {},
            "claims": [],
            "cadence": {},
        }

    first = brief.save(workspace_id="wsp_one", venture_id="ven_first", data=payload("First"))
    second = brief.save(workspace_id="wsp_one", venture_id="ven_second", data=payload("Second"))

    assert first["venture_id"] == "ven_first"
    assert second["venture_id"] == "ven_second"
    assert brief.active("wsp_one", "ven_first")["product"]["name"] == "First"
    assert brief.active("wsp_one", "ven_second")["product"]["name"] == "Second"
    assert brief.active("wsp_one") is None

    barrier = Barrier(2)
    def save_together(name: str) -> dict:
        barrier.wait()
        return brief.save(
            workspace_id="wsp_one", venture_id="ven_first", data=payload(name)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = sorted(
            result["version"]
            for result in pool.map(save_together, ("First revision A", "First revision B"))
        )
    assert versions == [2, 3]
    assert brief.active("wsp_one", "ven_first")["version"] == 3

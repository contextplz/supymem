"""Real migrated PG serializes concurrent takedown admission for one app."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select
from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401
from tests.unit.product.test_static_publication import _config


def test_four_concurrent_takedowns_share_one_committed_intent(journey_db, monkeypatch):
    from app.db.supymem_models import FounderApp, BuildRun, BuildArtifact, Deployment
    from app.db.models import Job
    from app.hosting import unpublish
    from app.hosting.service import HostingConfig
    from core.clock import utcnow
    monkeypatch.setattr(HostingConfig, "from_environment", classmethod(lambda _: _config()))
    d = journey_db
    suffix = uuid.uuid4().hex[:12]
    app_id, dep_id, artifact_id, build_id = [kind + "_" + suffix for kind in ("app", "dep", "bar", "brn")]
    now = utcnow()
    with d["factory"]() as session:
        session.add(FounderApp(id=app_id, workspace_id=d["wid"], venture_id=d["vid"], name="Studio", slug="studio-" + suffix,
                              kind="web", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id=build_id, app_id=app_id, workspace_id=d["wid"], venture_id=d["vid"], state="succeeded",
            trigger="test", agent="test", created_at=now, artifact_id=artifact_id,
            spec=json.dumps({"generation": 1, "artifact_capture": {"artifact_id": artifact_id, "sha256": "a" * 64}})))
        session.flush()
        session.add(BuildArtifact(id=artifact_id, build_run_id=build_id, app_id=app_id, workspace_id=d["wid"],
            kind="source_zip", s3_key="retained-source", sha256="a" * 64, size_bytes=10, created_at=now))
        session.add(Deployment(id=dep_id, app_id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            build_artifact_id=artifact_id, target="supymem_static", state="live", version=1,
            url=f"https://studio-{suffix}.supymem.com/", live_at=now))
        session.commit()
    barrier = Barrier(4)
    def contender(_):
        barrier.wait(timeout=10)
        return unpublish.queue(app_id, dep_id, actor=d["actor"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(contender, range(4)))
    assert len({result["unpublish"]["job_id"] for result in results}) == 1
    assert all(result["state"] == "live" for result in results)
    with d["factory"]() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "deploy.unpublish", Job.venture_id == d["vid"])).all()
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload_json)["deployment_id"] == dep_id
        assert session.get(Deployment, dep_id).state == "live"

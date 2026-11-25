import asyncio
from types import SimpleNamespace

from tests.unit.tasks.test_reasoning import deep_env as deep_env, credits_env as credits_env


def test_v2_pack_keeps_scoped_receipt_backed_outcomes_like_legacy(deep_env, monkeypatch):
    from app.db.supymem_models import BuildRun, FounderApp
    from app.hosting import service
    from app.memory import profile
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.model import MemoryKind, MemoryWrite, TaskRef
    from app.tenancy import bind
    from core.clock import utcnow
    env = deep_env
    artifact = SimpleNamespace(id="artifact", sha256="a" * 64)
    qa = SimpleNamespace(id="qa", report_digest="b" * 64)
    monkeypatch.setattr(service, "_accepted_source", lambda *args, **kwargs:
        (None, artifact, qa, {"manifest_digest": "c" * 64}, None))
    monkeypatch.setattr(profile, "selection_for", lambda *args, **kwargs: {"facts": [], "legacy": []})
    adapter = PostgresMemoryAdapter()
    ids = []
    for vid, wid in [(env.vid, env.wid), (env.other_vid, "ws_wallet:" + env.other_vid)]:
        run_id, app_id = "build_" + vid, "app_" + vid
        with env.db()() as session:
            session.add(FounderApp(id=app_id, workspace_id=wid, venture_id=vid, slug=app_id,
                name=vid, kind="static", created_at=utcnow(), updated_at=utcnow()))
            session.flush()
            session.add(BuildRun(id=run_id, app_id=app_id, workspace_id=wid, venture_id=vid,
                trigger="founder", agent="fixture", state="accepted"))
            session.commit()
        statement = f"Static source {run_id} was accepted with source-linked QA; publication is separate."
        with bind(workspace_id=wid, venture_id=vid, actor="per_wallet:" + vid):
            ids.append(asyncio.run(adapter.remember(MemoryWrite(venture_id=vid, kind=MemoryKind.OUTCOME,
                subject="build.accepted", statement=statement, evidence=["accepted_source:" + run_id],
                author="system", confidence=1.0, dedupe_key="receipt:" + run_id))))
    task = TaskRef(title="Static source accepted QA")
    with bind(workspace_id=env.wid, venture_id=env.vid, actor="per_wallet:" + env.vid):
        monkeypatch.setattr(adapter, "_search_v2_enabled", lambda: False)
        legacy = asyncio.run(adapter.context_pack(venture_id=env.vid, task=task, token_budget=4000))
        monkeypatch.setattr(adapter, "_search_v2_enabled", lambda: True)
        modern = asyncio.run(adapter.context_pack(venture_id=env.vid, task=task, token_budget=4000))
        assert [hit.mem_id for hit in legacy.memories] == [hit.mem_id for hit in modern.memories] == [ids[0]]
        assert ids[1] not in modern.model_dump_json()
        empty = asyncio.run(adapter.context_pack(venture_id=env.vid, task=task, token_budget=0))
        assert not empty.memories and empty.omitted_memory_ids == [ids[0]]
        artifact.sha256 = "d" * 64
        expired = asyncio.run(adapter.context_pack(venture_id=env.vid, task=task, token_budget=4000))
        assert not expired.memories

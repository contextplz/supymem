"""Real saved generation/review paths, with provider and private storage offline."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import zipfile

import pytest

from tests.company_os_harness import append_idea_revision
from tests.unit.pagebuilder.test_api import (
    setup as setup,
    claimed,
    install_pipeline_stub,
)  # noqa: PLC0414


class Store:
    def __init__(self):
        self.objects, self.writes, self.reads = {}, 0, 0
        self.uncertain = False
        self.after_read = None

    def persist_import(self, blob, scope, *, kind, object_digest):
        from app.builder.model_proxy import BuildModelError

        self.writes += 1
        key = tuple(sorted(scope.items())), object_digest
        receipt = {
            "sha256": object_digest,
            "size_bytes": len(blob),
            "version_id": "immutable-test-version",
            "kind": kind,
            "object_digest": object_digest,
        }
        if key in self.objects:
            assert self.objects[key] == (blob, receipt)
        self.objects[key] = blob, receipt
        if self.uncertain:
            self.uncertain = False
            raise BuildModelError("import_store_outcome_uncertain")
        return receipt

    def read_import(self, receipt, scope, *, kind, object_digest):
        self.reads += 1
        blob, stored = self.objects[tuple(sorted(scope.items())), object_digest]
        assert receipt == stored and kind == "raw"
        if self.after_read:
            action, self.after_read = self.after_read, None
            action()
        return blob


@pytest.mark.parametrize("failure_stage", ["plan_repair_1", "plan_repair_2", "repair_1"])
def test_uncertain_repair_delivers_saved_candidate_and_fresh_improve_keeps_hold(setup, monkeypatch, failure_stage):
    from app.core.jobs import complete_job
    from app.credits.accounts import get_account
    from app.db.models import CreditReservation, Job
    from app.builder.artifact_store import S3ArtifactStore
    from app.pagebuilder import api, jobs, native_reference, pipeline, rendering, runtime
    from tests.unit.pagebuilder.test_api import response
    from tests.unit.pagebuilder.test_pipeline import nightshift_draft, review_payload, stub_render
    from llm_gateway import sole
    d, calls, store = setup, [], Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    monkeypatch.setattr(rendering, "require_renderer", lambda: None)
    def render(html, files):
        rendered = stub_render(html, files)
        for observation in rendered["observations"].values():
            observation["visibleControls"][1]["target"] = "https://gateway.supymem.com/public/waitlists/" + d["vid"]
            observation.update(checks={check: True for check in ("contrast", "image_clipping", "hero_cta_initial")},
                contrastSkipped=0, contrastUnresolved=[], imageClippingFailures=[],
                initialHeroCta={"visible": True, "withinViewport": True, "hitCoverage": True})
        return rendered
    monkeypatch.setattr(rendering, "render_page", render)
    _, draft = nightshift_draft()
    async def transmit(**kwargs):
        stage = kwargs["purpose"].partition(".")[2]
        calls.append(stage)
        if stage == failure_stage:
            raise TimeoutError("private provider details")
        if stage == "brief_direction_plan":
            if failure_stage.startswith("plan_repair_"):
                return response("invalid JSON")
            return response(json.dumps(draft))
        if stage == "plan_repair_1":
            rejected = json.loads(json.dumps(draft))
            rejected["brief"]["promise"] = "Nightshift sends automatic notifications to supervisors."
            return response(json.dumps(rejected))
        if stage.startswith("capability_review_"):
            return response(json.dumps(review_payload(kwargs["messages"][-1]["content"])))
        if stage == "code":
            return response("<!doctype html><html></html>")
        pytest.fail("unexpected paid call: " + stage)
    monkeypatch.setattr(sole, "transmit", transmit)
    started, payload = claimed(d)
    jobs.run(payload)
    with d["factory"]() as session:
        row = session.get(Job, started["job_id"])
        doc = json.loads(row.payload_json)
        assert doc["_runtime"]["state"] == "ready" and api._candidate_current(doc)
        receipt = doc["_runtime"]["operations"]["variant_0:" + failure_stage]
        hold = session.get(CreditReservation, receipt["usage"]["reservation_id"])
        assert receipt["state"] == "uncertain" and hold.status in {"open", "active", "reconciling"}
        assert native_reference._completed_candidate(session, row) == doc
        settled = json.loads(json.dumps(doc))
        recovered = settled["_runtime"]["operations"]["variant_0:" + failure_stage]
        recovered.update(state="completed", error_code=None)
        recovered["usage"]["settled"] = True
        assert api._candidate_current(settled)
    status = api.status(d["vid"], d["actor"], started["job_id"])
    assert status["state"] == "ready" and status["improvement_available"] and status["receipt"]["uncertain_outcomes"] == 1
    assert "private provider details" not in json.dumps(status)
    before = list(calls), get_account(d["vid"]).reserved_micro
    jobs.run(payload)
    assert (calls, get_account(d["vid"]).reserved_micro) == before
    with d["factory"]() as session:
        complete_job(session, started["job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
    improved = api.build(d["vid"], d["actor"], new_version_of=started["job_id"])
    assert improved["job_id"] != started["job_id"] and improved["state"] == "queued"
    assert (calls, get_account(d["vid"]).reserved_micro) == before


@pytest.fixture()
def candidate(setup, monkeypatch):
    from app.builder.artifact_store import S3ArtifactStore
    from app.core.jobs import complete_job
    from app.pagebuilder import jobs, native_reference
    from app.db.models import Job

    d, store = setup, Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    install_pipeline_stub(monkeypatch)
    started, payload = claimed(d)
    jobs.run(payload)
    native_reference.persist_job(started["job_id"], payload)
    with d["factory"]() as session:
        complete_job(
            session,
            started["job_id"],
            worker_id=payload["_worker_id"],
            attempt=payload["_job_attempt"],
        )
        session.commit()
        doc = json.loads(session.get(Job, started["job_id"]).payload_json)
    d.update(
        job_id=started["job_id"],
        payload=payload,
        store=store,
        app_id=doc["_runtime"]["analytics_app_id"],
    )
    return d


def plan(d):
    from app.admin.authn import CallerContext
    from app.builder.expectations import compile_inline
    from app.db.supymem_models import (
        DesktopDevice,
        ExecutionPlan,
        PlanMilestone,
        PlanTask,
    )
    from core.clock import utcnow

    acceptance = ["Nightshift concept"]
    expectations = compile_inline(
        {
            "product_name": "Nightshift",
            "evidence": [
                {"summary": acceptance[0], "required_text": acceptance, "actions": []}
            ],
        },
        acceptance,
    )
    structured = {
        "proposal": {
            "milestones": [
                {
                    "key": "landing",
                    "tasks": [
                        {
                            "key": "page",
                            "description": "Implement an approved concept",
                            "acceptance": acceptance,
                            "landing_expectations": expectations,
                        }
                    ],
                }
            ]
        },
        "entity_ids": {"tasks": {"landing:page": "plt_reference"}},
    }
    with d["factory"]() as session:
        session.add(
            ExecutionPlan(
                id="pln_reference",
                workspace_id=d["wid"],
                venture_id=d["vid"],
                revision_id=d["rid"],
                version=1,
                state="approved",
                approved_at=utcnow(),
                approved_by=d["actor"],
                created_at=utcnow(),
                structured=json.dumps(structured),
            )
        )
        session.flush()
        session.add(
            PlanMilestone(
                id="plm_reference",
                plan_id="pln_reference",
                workspace_id=d["wid"],
                venture_id=d["vid"],
                order=1,
                title="Landing",
                kind="landing",
                scope="in",
                acceptance=json.dumps(acceptance),
            )
        )
        session.flush()
        session.add(
            PlanTask(
                id="plt_reference",
                milestone_id="plm_reference",
                workspace_id=d["wid"],
                venture_id=d["vid"],
                order=1,
                title="Build the landing page",
                description="Implement the approved concept",
                state="pending",
                tool_hints='["coding.subscription_static"]',
                created_at=utcnow(),
            )
        )
        session.add(
            DesktopDevice(
                id="dev_reference",
                person_id=d["actor"],
                name="Reference device",
                platform="macos",
                app_version="0.1.84",
            )
        )
        session.commit()
    return CallerContext(
        d["actor"],
        "",
        "user",
        "/test",
        "POST",
        9999999999,
        "test",
        device_id="dev_reference",
    )


def order(d, caller):
    from app.build import device_orders

    return device_orders.prepare_from_chat(
        d["vid"],
        caller,
        "Build the landing page with Codex",
        pagebuilder_candidate={"job_id": d["job_id"], "variant": 0},
    )


@pytest.mark.parametrize("failures", [
    {"brief_direction_plan": "length"},
    {"capability_review_1": "length"},
    {"plan_repair_1": "length"},
    {"capability_review_2": "length"},
    {"capability_review_1": "length", "capability_review_3": "length", "capability_review_5": "length"},
    {"code": "length"},
    {"code": "length", "repair_1": "empty"},
])
def test_settled_rejected_output_with_real_bounded_repair_can_stage_and_prepare(
    setup, monkeypatch, failures,
):
    """Real pipeline, meter and receipts; only provider, render and store are offline."""
    from app.builder.artifact_store import S3ArtifactStore
    from app.core.jobs import complete_job
    from app.db.models import CreditReservation, Job
    from app.pagebuilder import api, jobs, native_reference, pipeline, rendering, runtime
    from tests.unit.pagebuilder.test_api import response
    from tests.unit.pagebuilder.test_pipeline import fixture_html, nightshift_draft, review_payload, stub_render
    from llm_gateway import sole

    d, store, stages = setup, Store(), []
    raw_inputs, raw_plan = nightshift_draft()
    with d["factory"]() as session:
        d["rid"] = append_idea_revision(session, d["rid"], summary=raw_inputs["idea"]).id
        _revision, inputs = api.snapshot(session, d["vid"], d["wid"])
        session.commit()
    typed_inputs = pipeline.PageInput.model_validate(inputs)
    typed_plan = pipeline.prepare_plan(typed_inputs, lambda *_args: json.dumps(raw_plan))
    html = fixture_html(typed_plan, typed_inputs)
    semantic_repair = bool(set(failures) & {"plan_repair_1", "capability_review_2", "capability_review_5"})
    claim = "The concept would retain searchable shift history."
    bad_plan = json.loads(json.dumps(raw_plan))
    bad_plan["brief"]["proof_points"].append(claim)

    async def provider(**kwargs):
        stage = kwargs["purpose"].removeprefix("pagebuilder.")
        stages.append(stage)
        prompt = kwargs["messages"][-1]["content"]
        if stage.startswith("capability_review_"):
            findings = ([{"field_id": "brief.proof_points[3]", "claim": claim,
                         "reason": "This outcome is not confirmed by the founder's original idea or answers."}]
                        if semantic_repair and claim in prompt else [])
            content = json.dumps(review_payload(prompt, findings))
        elif stage == "brief_direction_plan" or stage.startswith("plan_repair_"):
            keep_bad = semantic_repair and (stage == "brief_direction_plan"
                or "capability_review_5" in failures and stage == "plan_repair_1")
            content = json.dumps(bad_plan if keep_bad else raw_plan)
        else:
            content = html
        reply = response("" if failures.get(stage) == "empty" else content)
        if failures.get(stage) == "length":
            reply.choices[0].finish_reason = "length"
        return reply

    def render(content, *_args):
        result = stub_render(content)
        for observation in result["observations"].values():
            observation["visibleControls"][1]["target"] = typed_inputs.waitlist_url
            observation.update(checks={"contrast": True, "image_clipping": True, "hero_cta_initial": True},
                contrastSkipped=0, contrastUnresolved=[], imageClippingFailures=[],
                initialHeroCta={"visible": True, "withinViewport": True, "hitCoverage": True})
        return result

    monkeypatch.setattr(sole, "transmit", provider)
    monkeypatch.setattr(rendering, "require_renderer", lambda: None)
    monkeypatch.setattr(rendering, "render_page", render)
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    started, payload = claimed(d)
    jobs.run(payload)
    with d["factory"]() as session:
        complete_job(session, started["job_id"], worker_id=payload["_worker_id"], attempt=payload["_job_attempt"])
        session.commit()
        doc = json.loads(session.get(Job, started["job_id"]).payload_json)
        for stage, reason in failures.items():
            operation = doc["_runtime"]["operations"]["variant_0:" + stage]
            assert operation["state"] == "completed" and operation["usage"]["settled"]
            assert operation["error_code"] == ("pagebuilder_output_truncated" if reason == "length" else "pagebuilder_invalid_output")
            assert session.get(CreditReservation, operation["usage"]["reservation_id"]).status == "settled"
    d.update(job_id=started["job_id"], app_id=doc["_runtime"]["analytics_app_id"], store=store)
    caller = plan(d)
    selection = {"job_id": d["job_id"], "variant": 0}
    binding = native_reference.resolve_candidate(d["vid"], caller, selection)
    assert order(d, caller)["manifest"]["coding_verification"]["pagebuilder_candidate"] == binding
    assert len(store.objects) == 1 and api._candidate_current(doc)
    assert all(stages.count(stage) == 1 for stage in stages)
    receipt = api.result(d["vid"], d["actor"], d["job_id"])["receipt"]
    assert receipt["calls"] == len(stages) <= api.CALLS_PER_VARIANT
    assert receipt["spent_credits"] > 0 and receipt["reserved_micro"] == 0
    native_reference.persist_job(d["job_id"], store=store)
    assert api.result(d["vid"], d["actor"], d["job_id"])["receipt"] == receipt

    # Known output rejection is not permission to accept uncertain or absent repairs.
    failed_key = "variant_0:" + next(iter(failures))
    for change in ("uncertain", "unsettled", "unrepaired", "foreign_variant", "empty_repair", "dirty_final_review", "over_ceiling"):
        changed = json.loads(json.dumps(doc))
        operations = changed["_runtime"]["operations"]
        failed = operations[failed_key]
        successor_stage = {"brief_direction_plan": "plan_repair_1", "plan_repair_1": "plan_repair_2",
            "capability_review_1": "capability_review_2", "capability_review_2": "capability_review_3",
            "code": "repair_2" if "repair_1" in failures else "repair_1"}[next(iter(failures))]
        successor = "variant_0:" + successor_stage
        if change == "uncertain":
            failed["state"] = "uncertain"
        elif change == "unsettled":
            failed["usage"]["settled"] = False
        elif change == "unrepaired":
            del operations[successor]
        elif change == "foreign_variant":
            operations[successor.replace("variant_0:", "variant_1:")] = operations.pop(successor)
        elif change == "empty_repair":
            operations[successor]["response"] = {"content": ""}
        elif change == "dirty_final_review":
            final = changed["result"]["bundles"][0]["qa"]["capability_grounding"]["reviews"][-1]
            if "capability_review_1" in failures:
                approved_stage = doc["result"]["bundles"][0]["qa"]["capability_grounding"]["reviews"][-1]["stage"]
                operations["variant_0:capability_review_1"]["response"] = operations["variant_0:" + approved_stage]["response"]
                final["stage"] = "capability_review_1"
                assert api._candidate_current(changed)  # valid content cannot clear its rejected receipt
            else:
                operations["variant_0:" + final["stage"]]["error_code"] = "pagebuilder_invalid_output"
        elif change == "over_ceiling":
            failed["error_code"] = "pagebuilder_provider_usage_above_ceiling"
        with d["factory"]() as session:
            session.get(Job, d["job_id"]).payload_json = json.dumps(changed)
            session.commit()
        with pytest.raises(ValueError, match=native_reference.INVALID):
            native_reference.resolve_candidate(d["vid"], caller, selection)
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).payload_json = json.dumps(doc)
        session.commit()
    assert json.loads(runtime.read(d["job_id"]).payload_json) == doc


def test_reference_is_immutable_idempotent_and_separate_from_native_acceptance(
    candidate, tmp_path,
):
    from app.build import device_orders
    from app.pagebuilder import native_reference

    d, caller = candidate, plan(candidate)
    from app.db.models import Job
    with d["factory"]() as session:
        assert json.loads(session.get(Job, d["job_id"]).payload_json)["result"]["ready_delivered"] is True
    first = order(d, caller)
    assert order(d, caller)["id"] == first["id"]
    binding = first["manifest"]["coding_verification"]["pagebuilder_candidate"]
    assert binding == native_reference.resolve_candidate(
        d["vid"], caller, {"job_id": d["job_id"], "variant": 0}
    )
    assert (
        first["state"] == "pending"
        and "native_stop" not in first
        and "execution_id" not in first
    )
    original = d["store"].writes
    native_reference.persist_job(d["job_id"])
    assert d["store"].writes == original
    body = next(iter(d["store"].objects.values()))[0]
    assert hashlib.sha256(body).hexdigest() == binding["reference_sha256"]
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert set(archive.namelist()) == {
            "source.zip",
            "manifest.json",
            "SPEC.md",
            "REFERENCE.json",
        }
        identity = json.loads(archive.read("REFERENCE.json"))
        assert identity == {
            k: v for k, v in binding.items() if not k.startswith("reference_")
        }
        assert (
            hashlib.sha256(archive.read("source.zip")).hexdigest()
            == binding["source_sha256"]
        )
        assert (
            hashlib.sha256(archive.read("SPEC.md")).hexdigest()
            == binding["spec_sha256"]
        )
    from desktop_runtime.pagebuilder_reference import stage_reference
    workspace = tmp_path.resolve() / "native-reference-workspace"
    workspace.mkdir()
    relative, staged = stage_reference(workspace, binding, body)
    assert relative == f".reference/pagebuilder/{d['job_id']}/variant-0"
    assert (workspace / relative / "source/index.html").is_file() and not (workspace / "index.html").exists()
    assert any(item["path"] == "SPEC.md" for item in staged)
    assert device_orders._digest(first) != device_orders._digest(
        {
            **first,
            "manifest": {
                **first["manifest"],
                "coding_verification": {
                    **first["manifest"]["coding_verification"],
                    "pagebuilder_candidate": {**binding, "variant": 1},
                },
            },
        }
    )
    summary = device_orders.list_orders(d["vid"], caller)[0]
    assert (
        summary["artifact_profile"] == "static_landing_v1"
        and summary["task_id"] == "plt_reference"
    )


def test_storage_uncertainty_retries_only_saved_bytes_even_after_terminal_queue_failure(
    candidate,
):
    from app.builder.model_proxy import BuildModelError
    from app.core.jobs import RetryableError
    from app.db.models import Job, VentureDocument
    from app.pagebuilder import native_reference, runtime

    d = candidate
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        before = json.loads(row.payload_json)["_runtime"]["operations"]
        doc = json.loads(row.payload_json)
        doc["result"].pop("ready_delivered", None)
        row.payload_json = json.dumps(doc)
        row.status = "failed_dead"
        session.delete(
            session.get(VentureDocument, native_reference._reference_id(d["job_id"], 0))
        )
        session.commit()
    fresh = Store()
    fresh.uncertain = True
    with pytest.raises(RetryableError, match=native_reference.UNAVAILABLE):
        native_reference.persist_job(d["job_id"], store=fresh)
    with d["factory"]() as session:
        intent = json.loads(
            session.get(
                VentureDocument, native_reference._reference_id(d["job_id"], 0)
            ).provenance_json
        )
        assert intent["state"] == "persisting" and "receipt" not in intent
        assert "ready_delivered" not in json.loads(session.get(Job, d["job_id"]).payload_json)["result"]
    result = native_reference.persist_job(d["job_id"], store=fresh)
    assert len(result) == 1 and len(fresh.objects) == 1
    assert json.loads(runtime.read(d["job_id"]).payload_json)["result"]["ready_delivered"] is True
    assert (
        json.loads(runtime.read(d["job_id"]).payload_json)["_runtime"]["operations"]
        == before
    )

    def unavailable():
        raise BuildModelError("artifact_store_unavailable")

    from app.builder.artifact_store import S3ArtifactStore

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(S3ArtifactStore, "configured", unavailable)
        with pytest.raises(RetryableError, match=native_reference.UNAVAILABLE):
            native_reference.persist_job(d["job_id"])


def test_explicit_source_retry_recovers_same_metered_job_and_fences_old_requests(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.builder.artifact_store import S3ArtifactStore
    from app.core.jobs import RetryableError, complete_job, fail_job, lease
    from app.db.models import CreditAccount, Job
    from app.pagebuilder import api, jobs, native_reference, pipeline, runtime

    d, store = setup, Store()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    install_pipeline_stub(monkeypatch)
    started, payload = claimed(d)
    with d["factory"]() as session:
        session.get(Job, started["job_id"]).max_attempts = 1
        session.commit()
    store.uncertain = True
    with pytest.raises(RetryableError, match=native_reference.UNAVAILABLE):
        jobs.run(payload)
    with d["factory"]() as session:
        assert fail_job(session, started["job_id"], RetryableError(native_reference.UNAVAILABLE),
            worker_id=payload["_worker_id"], attempt=payload["_job_attempt"]) == "failed_dead"
        session.commit()
        original = session.get(Job, started["job_id"]).payload_json
    doc = json.loads(original)
    assert doc["_runtime"]["calls_used"] == 3
    assert all(op["usage"]["settled"] for op in doc["_runtime"]["operations"].values())
    d.update(job_id=started["job_id"], app_id=doc["_runtime"]["analytics_app_id"], store=store)
    caller = plan(d)
    before = api.result(d["vid"], d["actor"], d["job_id"])["receipt"]
    with d["factory"]() as session:
        account = session.get(CreditAccount, d["account"].id)
        credits_before = (account.spent_micro, account.reserved_micro)
    selection = {"job_id": d["job_id"], "variant": 0}
    with pytest.raises(ValueError, match=native_reference.INVALID):
        native_reference.resolve_candidate(d["vid"], caller, selection)

    def retry(attempt):
        return native_reference.retry_source(d["job_id"], person_id=d["actor"],
            venture_id=d["vid"], workspace_id=d["wid"], expected_attempt=attempt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(pool.map(retry, [1, 1]))
    assert [result["queue_state"] for result in duplicates] == ["pending", "pending"]
    with d["factory"]() as session:
        job = lease(session, "source-retry-worker", kinds=[api.KIND], n=1)[0]
        assert job.attempts == job.max_attempts == 2
        session.commit()
    with d["factory"]() as session:
        assert fail_job(session, job.id, RetryableError(native_reference.UNAVAILABLE),
            worker_id=job.worker_id, attempt=job.attempts) == "failed_dead"
        session.commit()
    assert retry(1)["queue_state"] == "failed_dead"  # replay cannot authorize another attempt
    with d["factory"]() as session:
        assert session.get(Job, d["job_id"]).max_attempts == 2
    assert retry(2)["queue_state"] == "pending"  # refreshed explicit request can retry storage

    def forbidden(*_args, **_kwargs):
        raise AssertionError("storage recovery must not call generation, renderer or provider")
    monkeypatch.setattr(pipeline, "generate_page", forbidden)
    monkeypatch.setattr(runtime, "job_model_call", forbidden)
    from app.pagebuilder import rendering
    monkeypatch.setattr(rendering, "require_renderer", forbidden)
    with d["factory"]() as session:
        job = lease(session, "source-retry-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    recovery = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    jobs.run(recovery)
    with d["factory"]() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
        row = session.get(Job, job.id)
        assert row.status == "succeeded" and row.attempts == row.max_attempts == 3
        assert json.loads(row.payload_json) == {**doc, "result": {**doc["result"], "ready_delivered": True}}
        account = session.get(CreditAccount, d["account"].id)
        assert (account.spent_micro, account.reserved_micro) == credits_before
    assert retry(1)["queue_state"] == "succeeded"
    assert api.result(d["vid"], d["actor"], d["job_id"])["receipt"] == before
    binding = native_reference.resolve_candidate(d["vid"], caller, selection)
    assert order(d, caller)["manifest"]["coding_verification"]["pagebuilder_candidate"] == binding
    assert len(store.objects) == 1


@pytest.mark.parametrize("change", ["foreign", "sibling", "stale", "unconfirmed", "uncertain", "unsettled", "hold_unsettled", "source"])
def test_source_retry_rejects_foreign_stale_uncertain_or_changed_generation(candidate, change):
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaSession
    from app.pagebuilder import native_reference

    d = candidate
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        doc = json.loads(row.payload_json)
        row.status = "failed_dead"
        if change == "foreign":
            doc["_actor_subject"] = "other-original-founder"
        elif change == "sibling":
            row.venture_id = "other_venture"
        elif change == "stale":
            session.get(IdeaSession, d["sid"]).current_revision_id = "new_revision"
        elif change == "unconfirmed":
            session.get(IdeaSession, d["sid"]).state = "clarifying"
        elif change == "uncertain":
            next(iter(doc["_runtime"]["operations"].values()))["state"] = "uncertain"
        elif change == "unsettled":
            next(iter(doc["_runtime"]["operations"].values()))["usage"]["settled"] = False
        elif change == "hold_unsettled":
            usage = next(iter(doc["_runtime"]["operations"].values()))["usage"]
            session.get(CreditReservation, usage["reservation_id"]).status = "reconciling"
        elif change == "source":
            doc["result"]["bundles"][0]["source_zip"] = "invalid frozen source"
        row.payload_json = json.dumps(doc)
        session.commit()
        attempt, maximum = row.attempts, row.max_attempts
    with pytest.raises((KeyError, ValueError)):
        native_reference.retry_source(d["job_id"], person_id=d["actor"], venture_id=d["vid"],
            workspace_id=d["wid"], expected_attempt=attempt)
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        assert (row.status, row.attempts, row.max_attempts) == ("failed_dead", attempt, maximum)


@pytest.mark.parametrize(
    "change",
    [
        "foreign_job",
        "sibling_job",
        "unsettled",
        "provider_uncertain",
        "missing_code",
        "hold_unsettled",
        "old_policy",
        "source_mutation",
        "spec_mutation",
        "oversize_spec",
        "bad_variant",
        "revision_changed",
        "revision_unconfirmed",
        "plan_revision",
        "plan_superseded",
        "contract_changed",
        "app_deleted",
        "app_changed",
        "archived",
        "publication_changed",
    ],
)
def test_prepare_or_claim_rejects_foreign_stale_or_unproved_candidate(
    candidate, change
):
    from app.build import device_orders
    from app.db.models import CreditReservation, Job, Venture
    from app.db.supymem_models import ExecutionPlan, FounderApp, IdeaSession

    d, caller = candidate, plan(candidate)
    initial = order(d, caller)
    with d["factory"]() as session:
        row = session.get(Job, d["job_id"])
        doc = json.loads(row.payload_json)
        if change == "foreign_job":
            doc["_actor_subject"] = "other"
        if change == "sibling_job":
            row.venture_id = "sibling"
        if change == "unsettled":
            doc["_runtime"]["operations"]["variant_0:code"]["usage"]["settled"] = False
        if change == "provider_uncertain":
            doc["_runtime"]["operations"]["variant_0:code"]["state"] = "uncertain"
        if change == "missing_code":
            del doc["_runtime"]["operations"]["variant_0:code"]
        if change == "hold_unsettled":
            rid = doc["_runtime"]["operations"]["variant_0:code"]["usage"]["reservation_id"]
            session.get(CreditReservation, rid).status = "reconciling"
        if change == "old_policy":
            doc["_runtime"]["quote"]["request_policy"] = "obsolete"
        if change == "source_mutation":
            doc["result"]["bundles"][0]["source_zip"] = "Zm9yZ2Vk"
        if change == "spec_mutation":
            doc["result"]["bundles"][0]["spec_markdown"] += "Fake acceptance"
        if change == "oversize_spec":
            doc["result"]["bundles"][0]["spec_markdown"] = "x" * 65537
        if change == "revision_changed":
            session.get(IdeaSession, d["sid"]).current_revision_id = "new-revision"
        if change == "revision_unconfirmed":
            session.get(IdeaSession, d["sid"]).state = "open"
        if change == "plan_revision":
            session.get(ExecutionPlan, "pln_reference").revision_id = "old-revision"
        if change == "plan_superseded":
            session.get(ExecutionPlan, "pln_reference").superseded_by = "new-plan"
        if change == "contract_changed":
            saved = session.get(ExecutionPlan, "pln_reference")
            data = json.loads(saved.structured)
            data["proposal"]["milestones"][0]["tasks"][0]["landing_expectations"][
                "contract"
            ]["required_text"] = ["A different approved promise"]
            saved.structured = json.dumps(data)
        if change == "app_deleted":
            session.get(FounderApp, d["app_id"]).deleted_at = device_orders.utcnow()
        if change == "app_changed":
            doc["_runtime"]["analytics_app_id"] = "foreign-app"
        if change == "archived":
            session.get(Venture, d["vid"]).archived_at = device_orders.utcnow()
        if change == "publication_changed":
            session.get(FounderApp, d["app_id"]).slug = "changed-host"
        row.payload_json = json.dumps(doc)
        session.commit()
    if change == "bad_variant":
        with pytest.raises(ValueError):
            device_orders.prepare_from_chat(
                d["vid"],
                caller,
                "Build the landing page with Codex",
                pagebuilder_candidate={"job_id": d["job_id"], "variant": 1},
            )
    else:
        with pytest.raises((ValueError, KeyError)):
            device_orders.transition(
                d["vid"], caller, initial["id"], initial["review_digest"], "approve"
            )


def test_claimed_binary_endpoint_has_exact_account_device_and_current_scope(
    candidate, monkeypatch
):
    from app.api.device_orders_api import router
    from app.build import device_orders
    from app.pagebuilder import native_reference
    from fastapi import FastAPI
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from tests.unit.product.test_device_order_api import SECRET, headers

    d, caller = candidate, plan(candidate)
    prepared = order(d, caller)
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{d['vid']}/device-work-orders/{prepared['id']}/pagebuilder-reference"

    def signed(**kwargs):
        return headers(
            path,
            "GET",
            device=kwargs.get("device", caller.device_id),
            subject=kwargs.get("subject", caller.sub),
        )

    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=signed()).status_code == 409
        capability_path = path.rsplit("/", 1)[0] + "/capabilities"
        capable = client.get(capability_path, headers=headers(capability_path, "GET", device=caller.device_id, subject=caller.sub))
        assert capable.json()["pagebuilder_reference_v1"] is True
        other_device = client.get(capability_path, headers=headers(capability_path, "GET", device="dev_new", subject=caller.sub))
        assert other_device.json()["available"] is False and other_device.json()["pagebuilder_reference_v1"] is False
        approved = device_orders.transition(
            d["vid"], caller, prepared["id"], prepared["review_digest"], "approve"
        )
        assert client.get(path, headers=signed()).status_code == 409
        device_orders.transition(
            d["vid"], caller, prepared["id"], approved["review_digest"], "claim"
        )
        for other in (
            replace(caller, sub="other"),
            replace(caller, device_id="other-device"),
        ):
            with pytest.raises((KeyError, PermissionError, HTTPException)):
                native_reference.download(
                    d["vid"], other, prepared["id"], store=d["store"]
                )
        response = client.get(path, headers=signed())
        assert (
            response.status_code == 200
            and response.headers["content-type"] == "application/zip"
        )
        assert response.headers["cache-control"] == "no-store"
        assert response.content == next(iter(d["store"].objects.values()))[0]
        key, stored = next(iter(d["store"].objects.items()))
        d["store"].objects[key] = b"corrupt", stored[1]
        with pytest.raises(ValueError, match=native_reference.UNAVAILABLE):
            native_reference.download(d["vid"], caller, prepared["id"], store=d["store"])
        d["store"].objects[key] = stored

        def cancel():
            device_orders.transition(
                d["vid"], caller, prepared["id"], approved["review_digest"], "cancel"
            )

        d["store"].after_read = cancel
        with pytest.raises(ValueError):
            native_reference.download(
                d["vid"], caller, prepared["id"], store=d["store"]
            )


def test_old_runner_prepare_writes_no_selected_review_and_ordinary_order_still_works(candidate):
    from app.build import device_orders
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    from app.pagebuilder import native_reference
    d, caller = candidate, plan(candidate)
    with d["factory"]() as session:
        session.get(DesktopDevice, caller.device_id).app_version = "0.1.83"
        session.commit()
    with pytest.raises(ValueError, match=native_reference.RUNNER_UNAVAILABLE):
        order(d, caller)
    with d["factory"]() as session:
        assert session.query(VentureDocument).filter_by(kind=device_orders.KIND).count() == 0
    assert native_reference.resolve_candidate(d["vid"], caller, {"job_id": d["job_id"], "variant": 0})["job_id"] == d["job_id"]
    ordinary = device_orders.prepare_from_chat(d["vid"], caller, "Build the landing page with Codex")
    assert "pagebuilder_candidate" not in ordinary["manifest"]["coding_verification"]


@pytest.mark.parametrize("version", [None, "unknown", "0.1.83", "0.1.84-beta"])
def test_old_runner_cannot_claim_selected_candidate_and_legacy_omits_binding(
    candidate, version
):
    from app.build import device_orders
    from app.db.supymem_models import DesktopDevice
    from app.pagebuilder import native_reference

    d, caller = candidate, plan(candidate)
    prepared = order(d, caller)
    approved = device_orders.transition(
        d["vid"], caller, prepared["id"], prepared["review_digest"], "approve"
    )
    with d["factory"]() as session:
        session.get(DesktopDevice, caller.device_id).app_version = version
        session.commit()
    with pytest.raises(ValueError, match=native_reference.RUNNER_UNAVAILABLE):
        device_orders.transition(
            d["vid"], caller, prepared["id"], approved["review_digest"], "claim"
        )
    device_orders.transition(
        d["vid"], caller, prepared["id"], approved["review_digest"], "cancel"
    )
    ordinary = device_orders.prepare_from_chat(
        d["vid"], caller, "Build the landing page with Codex"
    )
    assert "pagebuilder_candidate" not in ordinary["manifest"]["coding_verification"]
    approved = device_orders.transition(
        d["vid"], caller, ordinary["id"], ordinary["review_digest"], "approve"
    )
    assert (
        device_orders.transition(
            d["vid"], caller, ordinary["id"], approved["review_digest"], "claim"
        )["state"]
        == "claimed"
    )


@pytest.mark.parametrize("field", ["source_zip", "files"])
def test_native_source_envelopes_are_bounded_before_owner_image_validation(setup, monkeypatch, field):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.pagebuilder import native_reference, api
    limits = replace(native_reference.DEFAULT_LIMITS, max_archive_bytes=3, max_file_bytes=3)
    monkeypatch.setattr(native_reference, "DEFAULT_LIMITS", limits)
    monkeypatch.setattr(api, "_candidate_current", lambda _: pytest.fail("oversized envelopes must not be decoded"))
    bundle = {"source_zip": "AAAA", "files": {"assets/hero.svg": "AAAA"}}
    bundle[field] = "A" * 8 if field == "source_zip" else {"assets/hero.svg": "A" * 8}
    row = SimpleNamespace(payload_json=json.dumps({"_runtime": {"state": "ready", "operations": {}}, "result": {"bundles": [bundle]}}))
    with pytest.raises(ValueError, match=native_reference.INVALID):
        native_reference._completed_candidate(None, row)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_artifacts_are_private_scoped_and_digest_checked(tmp_path, monkeypatch, domain):
    from tests.integration.test_postgres_auth_security import service_modules

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with service_modules("company_os"):
        from app.builder.model_proxy import BuildModelError
        from app.pagebuilder.local_artifacts import LocalArtifactStore

        scope = {"workspace_id": "workspace_" + domain, "venture_id": "venture_" + domain,
                 "app_id": "app_" + domain, "import_id": "page_job"}
        blob = ("<html>" + domain + "</html>").encode()
        digest = hashlib.sha256(blob).hexdigest()
        store = LocalArtifactStore.configured()
        receipt = store.persist_import(blob, scope, kind="raw", object_digest=digest)
        assert store.persist_import(blob, scope, kind="raw", object_digest=digest) == receipt
        assert store.read_import(receipt, scope, kind="raw", object_digest=digest) == blob
        path = next(tmp_path.glob("pagebuilder/artifacts/**/*.zip"))
        assert path.read_bytes() == blob and path.stat().st_mode & 0o077 == 0
        assert "path" not in receipt
        with pytest.raises(BuildModelError, match="receipt_invalid"):
            store.read_import(receipt, {**scope, "venture_id": "another_venture"}, kind="raw", object_digest=digest)
        with pytest.raises(BuildModelError, match="scope_invalid"):
            store.persist_import(blob, {**scope, "import_id": "../outside"}, kind="raw", object_digest=digest)
        path.write_bytes(b"corrupt")
        with pytest.raises(BuildModelError, match="artifact_changed"):
            store.read_import(receipt, scope, kind="raw", object_digest=digest)

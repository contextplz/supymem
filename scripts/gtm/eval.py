#!/usr/bin/env python3
"""Eight fixture ventures through the real GTM preparation service, at $0.

Run with the repository Python environment: python scripts/gtm/eval.py --report
docs/supymem/gtm/GTM_EVAL.md. All identities and .example sources are synthetic.
No inference-quality, live evidence, provider delivery, or demand claim follows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "services/company_os"), str(ROOT)]

FIXTURES = (
    ("DeployNotes", "A deployment handover checklist", "engineering leads at small SaaS companies"),
    ("GymRoster", "A climbing gym staff scheduling tool", "owners of independent climbing gyms"),
    ("RepairMarket", "A marketplace for vintage synthesizer repair", "independent instrument repair shops"),
    ("LessonPlan", "A lesson preparation checklist", "tutors operating small language schools"),
    ("FleetDesk", "A dispatch handover tool", "operations managers at regional delivery companies"),
    ("AccessLedger", "An access review evidence checklist", "security leads at software companies"),
    ("TrailMeet", "A hiking group planning service", "organizers of local outdoor clubs"),
    ("RinkRepairs", "An ice rink equipment repair log", "maintenance leads at community ice rinks"),
)


def _deny_network(*args, **kwargs):
    raise AssertionError("Fixture evaluation attempted network I/O")


async def evaluate() -> dict:
    from app.autopilot import outreach
    from app.core import jobs
    from app.db.models import GtmContact, Person, Venture, Workspace
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow

    result = []
    source_urls = {}
    now = utcnow()
    with db()() as session:
        session.add(Person(id="per_gtm_fixture", display_name="Fixture Founder", email="founder@fixture.example", created_at=now))
        session.commit()

    for index, (name, idea, audience) in enumerate(FIXTURES):
        print(f"Evaluating synthetic fixture {index + 1}/8: {name}", file=sys.stderr, flush=True)
        wid, vid = f"ws_gtm_fixture_{index}", f"ven_gtm_fixture_{index}"
        with db()() as session:
            session.add(Workspace(id=wid, kind="company", name=f"Fixture {name}", entry_mode="existing_company" if index % 2 == 0 else "idea",
                owner_id="per_gtm_fixture", settings_json=json.dumps({"entry_goal": "gtm"}), created_at=now, updated_at=now))
            session.add(Venture(id=vid, workspace_id=wid, name=name, raw_idea=idea, stage="idea", created_at=now, updated_at=now))
            session.commit()
        source_urls[vid] = None if index % 2 == 0 else f"https://{name.lower()}.example/product"
        rows = [{"email": f"avery{n}@{name.lower()}{n}.example", "organization": f"Fixture {name} Company {n}",
            "first_name": f"Fixture Avery {n}", "source_url": f"https://{name.lower()}{n}.example/contact",
            "why": f"Synthetic fixture company representing {audience}; not live research."} for n in range(3)]

        async def fake_discover(who, fixture_rows=rows):
            assert who == audience
            return [*fixture_rows, fixture_rows[0], {"email": "not-an-address", "organization": "Invalid fixture"}]

        with patch.object(journey, "authority", return_value="per_gtm_fixture"), patch.object(journey, "_discover", fake_discover), \
                patch.object(outreach, "site_url", side_effect=lambda v: source_urls[v]):
            queued = journey.update_icp(wid, vid, audience)
            duplicate = journey.prepare(wid, vid)
            checks = {"queue_dedupe": queued["job_id"] == duplicate["job_id"]}
            with db()() as session:
                claimed = jobs.lease(session, "fixture-eval", kinds=["gtm.journey.prepare"])
                session.commit()
            assert len(claimed) == 1, "Expected exactly one preparation job"
            job = claimed[0]
            payload = {**job.payload, "workspace_id": wid, "venture_id": vid, "_job_id": job.id,
                "_worker_id": job.worker_id, "_job_attempt": job.attempts}
            await journey.run_prepare(payload)
            with db()() as session:
                jobs.complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
                session.commit()
            held = journey.status(wid, vid)
            checks["public_contact_is_not_permission"] = len(held["prospects"]) == 3 and not held["drafts"] and all(not p["eligible"] for p in held["prospects"])

            # Explicit synthetic consent, never inferred from public email publication.
            for prospect in held["prospects"]:
                journey.patch_prospect(wid, vid, prospect["id"], {"country": "CA", "lawful_basis": "consent",
                    "lawful_basis_note": "Synthetic fixture express consent for deterministic evaluation only."})
            ready = journey.status(wid, vid)
            drafts, prospects = ready["drafts"], ready["prospects"]
            words = [len(d["body"].split()) for d in drafts]
            checks.update(
                icp_context_preserved=ready["icp"]["who"] == audience and ready["icp"]["confirmed"] is True,
                named_fixture_prospects=len(prospects) == 3 and all(p["name"].startswith("Fixture Avery") for p in prospects),
                fixture_sources_retained=all((p.get("source_url") or "").endswith(".example/contact") for p in prospects),
                review_only=len(drafts) == 3 and all(d.get("requires_batch_approval") is True and d.get("review_state") == "draft" for d in drafts),
                no_external_send=ready["results"]["sent"] == 0,
                draft_word_limit=bool(words) and max(words) <= 120,
                one_ask=all(d["body"].count("?") == 1 for d in drafts),
                no_fake_personalization=all(not any(phrase in d["body"].lower() for phrase in ("loved your post", "writing as a human", "no automation", "handwritten")) for d in drafts),
                no_page_required=all(("Product:" in d["body"]) == bool(source_urls[vid]) for d in drafts),
                no_implicit_approval=all(b["approved_count"] == 0 for b in ready["batches"]),
            )
            before = {d["doc_id"] for d in drafts}
            journey._draft_ready(wid, vid)
            checks["draft_dedupe"] = before == {d["doc_id"] for d in journey.status(wid, vid)["drafts"]}
            with db()() as session:
                contact = session.get(GtmContact, prospects[0]["id"])
                contact.do_not_contact = 1
                session.commit()
            checks["late_opt_out_visible"] = next(p for p in journey.status(wid, vid)["prospects"] if p["id"] == prospects[0]["id"])["eligible"] is False
            result.append({"venture": name, "entry": "existing product without page" if index % 2 == 0 else "published page fixture",
                "prospects": len(prospects), "drafts": len(drafts), "maximum_words": max(words, default=0), "checks": checks,
                "passed": all(checks.values())})
    return {"mode": "synthetic fixtures; network disabled", "provider_spend_usd": 0, "real_messages_sent": 0,
        "live_evidence_rate": None, "icp_inference_quality": None, "ventures": result,
        "passed": all(v["passed"] for v in result), "checks_passed": sum(sum(v["checks"].values()) for v in result),
        "checks_total": sum(len(v["checks"]) for v in result)}


def markdown(report: dict) -> str:
    rows = "\n".join(f"| {v['venture']} | {v['entry']} | {v['prospects']} | {v['drafts']} | {v['maximum_words']} | {'PASS' if v['passed'] else 'FAIL'} |" for v in report["ventures"])
    failures = [f"- {v['venture']}: {key}" for v in report["ventures"] for key, passed in v["checks"].items() if not passed]
    return f"""# GTM fixture evaluation

Run date: 5 October 2026. Command: `python scripts/gtm/eval.py --report docs/supymem/gtm/GTM_EVAL.md`.

Result: **{'PASS' if report['passed'] else 'FAIL'}**, {report['checks_passed']}/{report['checks_total']} deterministic checks across eight synthetic ventures. Provider/model spend: **$0**. Real messages sent: **0**.

All people, businesses, source pages, consent records and published-page contexts are explicit fixtures on reserved `.example` domains. An in-memory discovery adapter supplies them; the real preparation queue, lease checks, ingestion, permission gate, draft persistence and status service run against a temporary SQLite database. The actor adapter is stubbed for this structural service evaluation. This does not test API authentication, live discovery, inferred ICP quality, inbox placement, real delivery, or customer demand.

| Venture | Entry context | Fixture prospects | Drafts | Maximum words | Verdict |
|---|---|---:|---:|---:|---|
{rows}

Each fixture checks queue and draft dedupe, preserved founder-confirmed audience, named fixture contacts, source retention, unknown permission holding drafts, explicit synthetic consent enabling drafts, review-only state, no automatic send or approval, 120-word limit, one question, absence of fake personal claims, optional product links, and late opt-out eligibility.

Live prospect evidence rate: **not measured**. ICP inference quality: **not measured**; the evaluation verifies preservation of explicit founder audience input. Fixture source retention is a structural check and must never be represented as real prospect evidence quality.

{'Failures:\n' + chr(10).join(failures) if failures else 'No deterministic failures in this run.'}

Security and delivery proof are separate: run the unit/adversarial, API ownership, worker, and Postgres suites; review the release result note for the recipient-restricted production smoke. Production provider delivery remains the release agent's verification.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="supymem-gtm-eval-") as temp:
        env = {"COMPANY_OS_DB_URL": f"sqlite:///{temp}/eval.db", "DB_URL": f"sqlite:///{temp}/eval.db",
            "COMPANY_OS_DATA_DIR": temp, "COMPANY_OS_WORKING_MEMORY_PATH": f"{temp}/memory.db",
            "COMPANY_OS_BRAIN_GRAPH_PATH": f"{temp}/graph.db", "SANDBOX_MODE": "true", "LLM_OFFLINE": "true",
            "CHANNELS_DRY_RUN": "true", "EMAIL_PROVIDER": "log", "SUPYMEM_EMAIL_LIFECYCLE_ENABLED": "false",
            "SUPYMEM_OUTBOUND_EXECUTION_ENABLED": "false"}
        with patch.dict(os.environ, env), patch.object(socket.socket, "connect", _deny_network), \
                patch.object(socket, "getaddrinfo", _deny_network):
            report = asyncio.run(evaluate())
        from app import state
        if state._engine is not None:
            state._engine.dispose()
        state.reset_db_factory()
    print(json.dumps(report, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

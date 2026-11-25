#!/usr/bin/env python3
"""Run the CHAT_STEERING eval.

Default: real llm_gateway path (same resolver the server uses), with a funded
fixture credit account. Loads LLM_* from the repo .env and .env.local without
printing secrets.

Offline keyword-router regression (CI):
  CHAT_EVAL_OFFLINE=1 python scripts/eval_chat_steering.py
  # or: pytest tests/unit/chat/test_chat_steering_eval.py -q
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROMPTS = [
    ("1", "Hey, what's going on with LabHire right now? What do you need from me?"),
    ("2", "What did we decide about who LabHire is for?"),
    ("3", "What happened with the last Mac task you ran?"),
    ("4", "Is my Gmail connected? What can you do with it?"),
    ("5", "Why did the last build fail and what should we do about it?"),
    ("6", "Show me how the waitlist is doing."),
    ("7", "Draft a short outreach email to a first prospect for LabHire. Do not send it."),
    ("8", "Update the plan: our first milestone is 10 waitlist signups this week."),
    ("9", "Research three competitors of LabHire."),
    ("10", "What should I do next, in order?"),
    ("11", "Remember that I prefer short answers."),
    ("12", "What were we just talking about?"),
]

STATUS_MARKERS = (
    "**Now**",
    "**Needs you**",
    "**Next**",
    "**Being done now**",
    "**Blockers**",
)
BULLET_RE = re.compile(r"(?m)^\s*[-*•]\s+")
DASH_RE = re.compile(r"[\u2013\u2014\u2015\u2012\u2212]")


def _load_dotenv_files() -> list[str]:
    """Load LLM env from known local files. Never print values."""
    loaded: list[str] = []
    candidates = [
        ROOT / ".env",
        ROOT / ".env.local",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            key, _, val = s.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key.startswith(("LLM_", "OPENAI_", "ANTHROPIC_", "AZURE_")):
                continue
            if key in os.environ and os.environ.get(key):
                continue
            if val:
                os.environ[key] = val
        loaded.append(str(path))
    return loaded


def _key_reachable() -> tuple[bool, str]:
    offline = (os.environ.get("LLM_OFFLINE") or "").strip().lower() in {"1", "true", "yes"}
    if offline:
        return False, "LLM_OFFLINE is set"
    if not (os.environ.get("LLM_API_KEY") or "").strip():
        return False, "LLM_API_KEY not found in process env or local .env files"
    if not (os.environ.get("LLM_BASE_URL") or "").strip() and (
        os.environ.get("LLM_PROVIDER") or ""
    ).strip() in {"azure_foundry", "azure", ""}:
        return False, "LLM_BASE_URL not set for azure_foundry"
    return True, "ok"


_SPECULATION_RE = re.compile(
    r"\b("
    r"might happen on its own|"
    r"(?<!not )automatic(?:ally)?(?:\s+retry)?|"
    r"auto[- ]retry|"
    r"next (?:automated )?beat|"
    r"waiting on the next beat|"
    r"waiting for a worker|"
    r"worker (?:will|slot|becomes|become|available)|"
    r"on (?:the )?next beat"
    r")\b",
    re.I,
)


def _entity_ok(pid: str, body: str, kind: str | None) -> bool:
    # Ignore speaker stamp HTML when judging content.
    lower = re.sub(r"<!--employee:[^>]*-->", "", body).lower()
    if pid == "1":
        return "labhire" in lower and any(
            k in lower
            for k in ("need", "you", "attention", "build", "fail", "waitlist", "blocked", "nothing")
        )
    if pid == "2":
        return "engineer" in lower or "audience" in lower or "for" in lower
    if pid == "3":
        return "mac" in lower or "terminal" in lower or "task" in lower
    if pid == "4":
        return "gmail" in lower
    if pid == "5":
        return "fail" in lower and ("provider" in lower or "15.3594" in body or "credit" in lower)
    if pid == "6":
        return "waitlist" in lower and (
            "0" in body
            or "zero" in lower
            or "empty" in lower
            or "no " in lower
            or "any signup" in lower
            or "no signup" in lower
            or "doesn't have" in lower
            or "does not have" in lower
            or "haven't" in lower
            or "have not" in lower
        )
    if pid == "7":
        return kind in {"change", "confirmation", "text"} and (
            "draft" in lower or "email" in lower or "outreach" in lower or "queue" in lower
        )
    if pid == "8":
        return any(k in lower for k in ("queue", "milestone", "waitlist", "plan", "autopilot", "samira"))
    if pid == "9":
        return kind == "confirmation" or ("research" in lower and any(
            k in lower for k in ("confirm", "credit", "queue", "competitor")
        ))
    if pid == "10":
        return any(k in lower for k in ("next", "need", "you", "order", "open", "retry", "build"))
    if pid == "11":
        clean = re.sub(r"<!--employee:[^>]*-->", "", body).strip()
        return (
            "got it" in lower
            and "short" in lower
            and "i prefer" not in lower
            and not clean.lower().startswith("remembered:")
        )
    if pid == "11b":
        # After short-answer preference: keep the follow-up brief.
        plain = re.sub(r"<!--employee:[^>]*-->", "", body).strip()
        return len(plain) <= 420 and plain.count("\n\n") <= 2
    if pid == "12":
        return any(k in lower for k in ("talk", "waitlist", "gmail", "build", "research", "earlier", "just", "covering", "short"))
    return bool(body.strip())


def _judge(pid: str, body: str, kind: str | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not body.strip():
        reasons.append("empty")
    if any(m in body for m in STATUS_MARKERS):
        reasons.append("status_card")
    if BULLET_RE.search(body) and kind not in {"confirmation"} and pid != "9":
        reasons.append("bullets")
    if DASH_RE.search(body):
        reasons.append("em_or_en_dash")
    if pid in {"5", "10"} and _SPECULATION_RE.search(body):
        reasons.append("speculated_auto_retry")
    if not _entity_ok(pid, body, kind):
        reasons.append("not_grounded_or_off_question")
    # Invented-fact heuristic: refuse obviously fake credit/email if not in fixture.
    if "founder-secret@example.com" in body:
        reasons.append("leaked_secret_email")
    return (not reasons), reasons


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _seed_fixture(vid: str, wid: str) -> None:
    import json as _json

    from app.db.models import GtmEmailAccount, IdeaBrief, VentureDocument
    from app.db.supymem_models import BuildRun, FounderApp
    from app.state import MEM, db
    from core.clock import utcnow
    from core.ids import new_id

    MEM[f"website:{vid}"] = {"url": "https://labhire.example", "published": True, "live": True}

    factory = db()
    with factory() as session:
        session.add(
            IdeaBrief(
                id=new_id("ibr"),
                venture_id=vid,
                domain="jobs",
                business_model="marketplace",
                build_archetype="web",
                audience="QA engineers looking for roles that match their stack",
                regulatory_surface="none",
                novelty="medium",
                complexity_score=0.4,
                uncertainty_score=0.5,
                build_feasibility=0.7,
                complexity_factors_json="{}",
                research_notes_json="{}",
                classifier_version="eval",
                created_at=utcnow(),
            )
        )
        app_id = new_id("app")
        session.add(
            FounderApp(
                id=app_id,
                workspace_id=wid,
                venture_id=vid,
                name="LabHire",
                slug=f"labhire-{app_id[-8:]}",
                kind="web",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.flush()
        session.add(
            BuildRun(
                id=new_id("bld"),
                app_id=app_id,
                workspace_id=wid,
                venture_id=vid,
                trigger="eval",
                agent="managed",
                state="failed",
                spec=_json.dumps({"failure_reason": "provider down"}),
                credits_used=153_594,
                error_code="provider_down",
                exit_summary="provider down",
                runtime_version=1,
                created_at=utcnow(),
            )
        )
        session.add(
            GtmEmailAccount(
                id=new_id("gea"),
                workspace_id=wid,
                provider="gmail",
                email="founder-secret@example.com",
                composio_user_id="cu_eval",
                connected_account_id="ca_eval",
                status="active",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.add(
            VentureDocument(
                id=new_id("dwo"),
                venture_id=vid,
                kind="device_work_order_v1",
                title="Verify venture workspace in terminal",
                path=None,
                body_md=None,
                provenance_json=_json.dumps(
                    {"status": "completed", "manifest": {"task_id": "t1"}}
                ),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.commit()

    from app.credits.accounts import ensure_account

    ensure_account(vid, workspace_id=wid)


def _write_report(rows: list[dict], latencies: list[float], skip_counts: dict, out_md: Path) -> None:
    passed = sum(1 for r in rows if r["pass"])
    sorted_lat = sorted(latencies)
    p50 = _percentile(sorted_lat, 50)
    p95 = _percentile(sorted_lat, 95)
    lines = [
        "# CHAT_EVAL_REAL",
        "",
        f"Pass: **{passed}/12**",
        f"Latency (s): p50={p50:.3f}, p95={p95:.3f}, n={len(latencies)}",
        f"Model-skip counters during run: `{json.dumps(skip_counts, sort_keys=True)}`",
        "",
        "| # | Pass | Latency s | Kind | Prompt | Reply |",
        "|---|------|-----------|------|--------|-------|",
    ]
    for r in rows:
        reply = (r["after"] or "").replace("|", "\\|").replace("\n", " ")
        prompt = (r["prompt"] or "").replace("|", "\\|")
        lines.append(
            f"| {r['id']} | {'PASS' if r['pass'] else 'FAIL'} | {r['latency_s']:.3f} | "
            f"{r.get('kind') or ''} | {prompt} | {reply} |"
        )
    lines.extend(["", "## Raw replies", ""])
    for r in rows:
        lines.append(f"### #{r['id']} ({'PASS' if r['pass'] else 'FAIL'})")
        lines.append(f"**Prompt:** {r['prompt']}")
        lines.append("")
        lines.append(r["after"] or "(empty)")
        lines.append("")
        if r.get("fail_reasons"):
            lines.append(f"Fail reasons: {', '.join(r['fail_reasons'])}")
            lines.append("")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run_real(report_md: Path) -> int:
    loaded = _load_dotenv_files()
    ok, why = _key_reachable()
    if not ok:
        print(f"REAL_EVAL_BLOCKED: {why}", file=sys.stderr)
        print(
            "Need: LLM_API_KEY (+ LLM_BASE_URL / LLM_PROVIDER for Foundry) in "
            ".env or process env; LLM_OFFLINE unset/false. Do not paste the key into chat.",
            file=sys.stderr,
        )
        print(f"dotenv_files_considered_present={loaded}", file=sys.stderr)
        return 2

    # Ensure we are not in offline mode for the real path.
    os.environ["LLM_OFFLINE"] = "0"

    sys.path.insert(0, str(ROOT / "packages"))
    sys.path.insert(0, str(ROOT / "services" / "company_os"))
    # Prefer pytest harness path resolution used elsewhere.
    os.environ.setdefault("PYTHONPATH", f"{ROOT}/packages:{ROOT}/services/company_os")

    from fastapi.testclient import TestClient

    # Import after path setup.
    repo_tests = ROOT / "tests"
    if str(repo_tests) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tests.company_os_harness import load_company_os_app

    with tempfile.TemporaryDirectory(prefix="chat-eval-real-") as tmp:
        tmp_path = Path(tmp)
        os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{tmp_path}/eval.db"
        os.environ["COMPANY_OS_DATA_DIR"] = str(tmp_path)
        os.environ["COMPANY_OS_MEMORY_PATH"] = str(tmp_path / "mem.sqlite3")
        os.environ["ONBOARDING_SYNC"] = "0"

        main = load_company_os_app()
        from app import state as state_mod
        from app.chat.assistant import (
            assistant_model_skip_counts,
            reset_assistant_model_skip_counts,
        )

        state_mod.reset_db_factory()
        state_mod.MEM.clear()
        reset_assistant_model_skip_counts()

        with TestClient(main.create_app()) as client:
            created = client.post(
                "/ventures",
                json={"raw_idea": "LabHire helps QA engineers find matching roles", "name": "LabHire"},
            )
            if created.status_code != 201:
                print(f"venture create failed: {created.status_code} {created.text}", file=sys.stderr)
                return 1
            payload = created.json()
            vid = payload["id"]
            wid = payload["workspace_id"]
            _seed_fixture(vid, wid)
            from app.ventures.state_brief import invalidate_venture_state_brief

            invalidate_venture_state_brief(vid)

            from app.chat.router import handle_message

            rows: list[dict] = []
            latencies: list[float] = []
            # After the remember prompt, prove the preference shortens the next answer.
            run_prompts = list(PROMPTS) + [
                ("11b", "Hey, what's going on with LabHire right now? What do you need from me?"),
            ]
            for pid, prompt in run_prompts:
                t0 = time.perf_counter()
                reply = await handle_message(vid, prompt)
                dt = time.perf_counter() - t0
                # Latency for LLM-path turns (instant keyword actions stay out of p50/p95).
                if pid in {"1", "2", "3", "4", "5", "6", "10", "12", "11b"}:
                    latencies.append(dt)
                body = str(reply.get("body_md") or "")
                kind = reply.get("kind")
                passed, reasons = _judge(pid, body, kind)
                rows.append(
                    {
                        "id": pid,
                        "prompt": prompt,
                        "after": body,
                        "kind": kind,
                        "pass": passed,
                        "fail_reasons": reasons,
                        "latency_s": dt,
                        "path": "llm" if dt > 0.05 else "unknown",
                    }
                )
                flag = "PASS" if passed else "FAIL"
                print(f"[{flag}] #{pid} {dt:.2f}s kind={kind} reasons={reasons}")

            skip_counts = assistant_model_skip_counts()
            # Core pass count is still out of 12; 11b is an extra preference proof.
            core = [r for r in rows if r["id"] != "11b"]
            _write_report(core, latencies, skip_counts, report_md)
            # Append 11b into the markdown raw section.
            extra = next(r for r in rows if r["id"] == "11b")
            with report_md.open("a", encoding="utf-8") as fh:
                fh.write("\n### #11b preference short-answer proof\n")
                fh.write(f"**Prompt:** {extra['prompt']}\n\n")
                fh.write(extra["after"] or "(empty)")
                fh.write("\n\n")
                fh.write(
                    f"Pass: {'PASS' if extra['pass'] else 'FAIL'} "
                    f"({len((extra['after'] or ''))} chars). "
                    f"Reasons: {', '.join(extra['fail_reasons']) or 'none'}\n"
                )
            json_path = report_md.with_suffix(".json")
            json_path.write_text(
                json.dumps(
                    {
                        "rows": rows,
                        "latencies_s": latencies,
                        "p50_s": _percentile(sorted(latencies), 50),
                        "p95_s": _percentile(sorted(latencies), 95),
                        "skip_counts": skip_counts,
                        "pass_count": sum(1 for r in core if r["pass"]),
                        "pass_count_with_11b": sum(1 for r in rows if r["pass"]),
                        "streaming_note": (
                            "Chat POST is request/response only; sole.transmit_stream exists "
                            "but chat_api has no SSE. Dock shows Ren typing indicator immediately."
                        ),
                        "website_url_note": (
                            "Eval fixture seeds https://labhire.example; production MEM website "
                            "url comes from firstlook/onboarding publish path (real URL)."
                        ),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            passed = sum(1 for r in core if r["pass"])
            short_ok = extra["pass"]
            print(f"PASS_COUNT={passed}/12")
            print(f"SHORT_PREF_PROOF={'PASS' if short_ok else 'FAIL'}")
            print(f"P50_S={_percentile(sorted(latencies), 50):.3f}")
            print(f"P95_S={_percentile(sorted(latencies), 95):.3f}")
            print(f"SKIP_COUNTS={json.dumps(skip_counts, sort_keys=True)}")
            print(f"REPORT_MD={report_md}")
            print(f"REPORT_JSON={json_path}")
            return 0 if passed >= 11 and short_ok else 1


def _run_offline_pytest() -> int:
    import subprocess

    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            str(ROOT / "tests" / "unit" / "chat" / "test_chat_steering_eval.py"),
            "-q",
            "--tb=short",
        ],
        cwd=str(ROOT),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run offline pytest keyword-router eval instead of the real model path.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("/private/tmp/supymem-analytics-night/docs/archive/orchestration-2026-09-23/CHAT_EVAL_REAL.md"),
        help="Markdown report path for the real eval.",
    )
    args = parser.parse_args()
    if args.offline or (os.environ.get("CHAT_EVAL_OFFLINE") or "").strip() in {"1", "true", "yes"}:
        return _run_offline_pytest()
    return asyncio.run(_run_real(args.report))


if __name__ == "__main__":
    raise SystemExit(main())

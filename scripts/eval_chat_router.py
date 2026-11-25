#!/usr/bin/env python3
"""Founder-message routing eval (370+ messages) against the real chat router.

  python scripts/eval_chat_router.py                    # regex only (baseline), offline answers
  python scripts/eval_chat_router.py --live             # LLM router live (DeepSeek via local .env LLM_*)
  python scripts/eval_chat_router.py --live --record    # also rewrite tests/fixtures/chat_eval/decisions.json
  python scripts/eval_chat_router.py --replay           # recorded decisions (what CI runs)

LLM_* are read from the repo .env and .env.local into process env only. Never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECISIONS = ROOT / "tests" / "fixtures" / "chat_eval" / "decisions.json"


def _load_env() -> None:
    for p in (ROOT / ".env", ROOT / ".env.local"):
        if not p.is_file():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            k, _, v = line.strip().partition("=")
            if k.startswith("LLM_") and v and not os.environ.get(k):
                os.environ[k] = v.strip().strip("\"'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--live-answers", action="store_true", help="also use the live model for assistant answers (slow)")
    ap.add_argument("--shard", default="", help="i/n: run every n-th case starting at i")
    ap.add_argument("--record-to", default="")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--cat", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    sys.path[:0] = [str(ROOT), str(ROOT / "packages"), str(ROOT / "services" / "company_os")]
    if args.live:
        _load_env()
    os.environ["CHAT_ROUTER_MODE"] = "live" if args.live else ("replay" if args.replay else "off")
    os.environ["CHAT_ROUTER_DECISIONS"] = str(DECISIONS)
    os.environ["CHAT_ROUTER_RECORD"] = "1" if args.record else "0"
    os.environ["CHAT_ROUTER_RECORD_TO"] = args.record_to
    os.environ["LLM_OFFLINE"] = "0" if (args.live and args.live_answers) else "1"
    if args.live and not args.live_answers:
        os.environ["LLM_OFFLINE"] = "0"  # the router needs the key; assistant answers are stubbed below

    from fastapi.testclient import TestClient

    from tests.company_os_harness import load_company_os_app
    from tests.fixtures.chat_eval.cases import C
    from tests.fixtures.chat_eval import runner

    with tempfile.TemporaryDirectory(prefix="chat-router-eval-") as tmp:
        os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{tmp}/eval.db"
        os.environ["COMPANY_OS_DATA_DIR"] = tmp
        os.environ["COMPANY_OS_MEMORY_PATH"] = f"{tmp}/mem.sqlite3"
        os.environ["ONBOARDING_SYNC"] = "0"
        main_mod = load_company_os_app()
        from app import state as state_mod

        state_mod.reset_db_factory()
        state_mod.MEM.clear()
        with TestClient(main_mod.create_app()) as client:
            made = client.post("/ventures", json={"raw_idea": "LabHire helps QA engineers find matching roles", "name": "LabHire"})
            vid, wid = made.json()["id"], made.json()["workspace_id"]
            from app.credits.accounts import ensure_account

            ensure_account(vid, workspace_id=wid)
            if not args.live_answers:
                import app.chat.assistant as assistant

                async def _none(*_a, **_k):
                    return ""

                assistant._complete = _none
            from app.chat.router import handle_message

            cases = [c for c in C if not args.cat or c[0] == args.cat]
            if args.shard:
                i, n = (int(x) for x in args.shard.split("/"))
                cases = cases[i::n]
            if args.limit:
                cases = cases[: args.limit]
            res = runner.run_all(handle_message, vid, cases,
                                 progress=lambda i, r: print(f"[{i + 1}/{len(cases)}] {'ok ' if runner.judge(r['expected'], r['seen'], bool(r['cards']), runner._spent(r), r['kind']) else 'BAD'} {r['text'][:60]!r} -> {r['seen']} {r['cards']}", file=sys.stderr))
            s = runner.score(res)
            fails = s.pop("fails")
            from app.chat import llm_router

            rs = sorted(llm_router.ROUTE_SECONDS)
            s["router_model_calls"] = len(rs)
            if rs:
                s["router_latency_p50"], s["router_latency_p95"] = round(rs[len(rs) // 2], 2), round(rs[int(len(rs) * .95) - 1], 2)
            print(json.dumps(s, indent=1))
            for r in fails:
                print(f"FAIL [{r['cat']}] {r['text'][:90]!r}\n   expected={r['expected']} seen={r['seen']} cards={r['cards']} "
                      f"delta={r['delta']} kind={r['kind']}\n   body={r['body'][:140]!r}")
            for r in res:
                if r["false_claim"]:
                    print(f"FALSE-CLAIM {r['text'][:80]!r}: {r['body'][:160]!r}")
            if args.json:
                Path(args.json).write_text(json.dumps({"summary": s, "rows": res}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

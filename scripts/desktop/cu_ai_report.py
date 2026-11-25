"""Render /tmp/cu_suite.jsonl (written by cu_ai_suite.py) as a markdown table for the PR / QA doc."""
import json
import sys
from pathlib import Path

rows = [json.loads(line) for line in Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cu_suite.jsonl").read_text().splitlines()]
print("| trial | result | steps | wall s | model calls | credits (micro) | ms/step obs / model / act | decoy | foreground escalations | check |")
print("|---|---|---|---|---|---|---|---|---|---|")
for r in rows:
    ms = r.get("avg_ms") or {}
    verdict = "PASS" if r.get("passed") else f"FAIL ({r.get('reason') or r.get('outcome')})"
    decoy = f"{'untouched' if r.get('decoy_untouched') else 'TOUCHED'}, frontmost {r.get('calculator_frontmost_pct')}%"
    print(f"| {r['trial']} | {verdict} | {r.get('steps')} | {r.get('seconds')} | {r.get('model_calls')} | "
          f"{r.get('cost_micro_credits')} | {ms.get('observe')} / {ms.get('model')} / {ms.get('act')} | {decoy} | "
          f"{r.get('foreground_escalations')} | {str(r.get('verify'))[:90]} |")

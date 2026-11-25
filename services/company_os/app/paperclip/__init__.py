"""Paperclip control-plane algorithms, adapted for Company OS.

Upstream: https://github.com/paperclipai/paperclip (MIT, Paperclip AI).
Copyright (c) 2025 Paperclip AI. The MIT notice from ``vendor/paperclip/LICENSE``
applies to the algorithms copied into this package.

Paperclip is a Node.js + React control plane ("if OpenClaw is an employee,
Paperclip is the company"). We do **not** run that server, UI, adapters, or
Postgres schema in-process.

We take three process invariants from their code and execute them against
our planned-task queue, charter, and LLM usage ledger:

- Atomic issue checkout (``server/src/services/issues.ts`` ``checkout``)
- Goal fallback / ancestry (``server/src/services/issue-goal-fallback.ts``)
- Budget status + hard-stop (``server/src/services/budgets.ts``
  ``budgetStatusFromObserved``)
- Per-agent start lock / wakeup coalesce (``server/src/services/agent-start-lock.ts``,
  ``concurrencyPolicy: coalesce_if_active``)

Same pattern as ``app.gbrain`` / ``app.gstack``.
"""

from app.paperclip.budget import budget_status_from_observed, hard_stop_for_venture
from app.paperclip.checkout import claim_next_open_task, claim_task
from app.paperclip.goals import ancestry_markdown, resolve_issue_goal_id, resolve_next_issue_goal_id
from app.paperclip.wakeup import note_coalesced_wakeup, take_coalesced_wakeup, with_venture_start_lock

__all__ = [
    "ancestry_markdown",
    "budget_status_from_observed",
    "claim_next_open_task",
    "claim_task",
    "hard_stop_for_venture",
    "note_coalesced_wakeup",
    "resolve_issue_goal_id",
    "resolve_next_issue_goal_id",
    "take_coalesced_wakeup",
    "with_venture_start_lock",
]

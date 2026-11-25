"""Finance agent — summarizes recorded observations without inventing snapshots."""

from __future__ import annotations

from typing import Any

from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent


class FinanceAgent(BaseOpsAgent):
    agent_type = "finance"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        workspace_id = context.get("workspace_id") or ""
        from app.ops.services import finance as finance_svc

        summary = finance_svc.get_summary(workspace_id) if workspace_id else {}
        snapshot_date = summary.get("last_snapshot_date")
        # A summarizer has no provider observation to persist. In particular,
        # missing data must never become a fresh, observed-zero snapshot.
        if snapshot_date is None:
            narrative = "Revenue, subscriber count and Stripe balance are unavailable: no recorded revenue snapshot."
        else:
            narrative = (
                f"Recorded snapshot {snapshot_date}: "
                f"MRR ${summary['mrr_cents'] / 100:.2f}, "
                f"ARR ${summary['arr_cents'] / 100:.2f}."
            )
        narrative += " Expenses and ad spend reflect recorded entries, not reconciled provider totals."
        return {
            **summary,
            "summary": narrative,
            "mrr_cents": summary.get("mrr_cents"),
            "arr_cents": summary.get("arr_cents"),
            "active_subscribers": summary.get("active_subscribers"),
            "stripe_balance_cents": summary.get("stripe_balance_cents"),
            "last_snapshot_date": snapshot_date,
            "revenue_basis": summary.get("revenue_basis", "unavailable"),
            "sandbox": ops_config.sandbox_mode(),
        }

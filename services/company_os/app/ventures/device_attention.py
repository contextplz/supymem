"""Read-only attention labels for unsettled Mac work orders."""

from __future__ import annotations


def pending_mac_reconciliation(body: dict) -> str | None:
    """A cancelled order can still need review before its hold is reconciled."""
    if (body.get("state") != "cancelled" or body.get("stop_reconciliation_required") is not True
            or body.get("reviewed_at")):
        return None
    invocations = body.get("invocations")
    if isinstance(invocations, dict) and any(
        isinstance(value, dict)
        and isinstance(value.get("effect"), dict)
        and value["effect"].get("state") == "uncertain"
        and isinstance(value.get("proposal"), dict)
        and value["proposal"].get("operation") == "click"
        for value in invocations.values()
    ):
        return "A Mac task has an uncertain click. Review its effect and credit reconciliation."
    return "The cancelled Mac task needs stop or usage reconciliation. Review its saved result."

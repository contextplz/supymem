"""Explicit synthetic billing proofs for fake model adapters, never live wallet evidence."""
from copy import deepcopy
import hashlib


def synthetic_receipt(options, invocation=None, estimate_micro=1, actual_micro=0, prompt=1, completion=1, cached=0, source="platform"):
    request_id = options.get("request_id") or "synthetic-untracked-request"
    key = hashlib.sha256(request_id.encode()).hexdigest()[:24]
    return {"source": source, "reservation_id": "synthetic_hold:" + key if source == "platform" else None,
        "account_id": "synthetic_wallet" if source == "platform" else None, "estimate_micro": estimate_micro,
        "workspace_id": options["workspace_id"], "venture_id": options["venture_id"], "purpose": options["purpose"],
        "model": "synthetic_model", "provider": "synthetic_provider", "request_id": request_id,
        "task_invocation": invocation, "actual_micro": actual_micro, "provider_actual_micro": actual_micro,
        "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached, "total_tokens": prompt + completion}


def checkpoint(response, options, receipt):
    callback = options.get("before_meter_exit")
    if callback is not None:
        callback(response, deepcopy(receipt))


class FakeRecovery:
    """Only confirm() creates a fake immutable proof, after the pre-exit hook."""
    def __init__(self):
        self.proofs = {}
        self.calls = []

    @staticmethod
    def _key(receipt):
        return receipt["workspace_id"], receipt["venture_id"], receipt["purpose"], receipt["request_id"]

    def confirm(self, receipt):
        from app.credits.errors import CreditsUnavailable
        from app.tasks.billing import _usage
        known = _usage(receipt)
        key = self._key(known)
        if key in self.proofs and self.proofs[key] != known:
            raise CreditsUnavailable("Synthetic billing proof changed.")
        self.proofs[key] = deepcopy(known)

    def reconcile_receipt(self, receipt, expected_binding):
        from app.credits.errors import CreditsUnavailable
        from app.tasks.billing import _receipt_binding, _usage
        from app.tasks import usage
        normalized = _usage(receipt)
        if (expected_binding != _receipt_binding(normalized)
                or self.proofs.get(self._key(normalized)) != normalized):
            raise CreditsUnavailable("Synthetic receipt is not confirmed for this exact request.")
        self.calls.append((deepcopy(normalized), deepcopy(expected_binding)))
        if normalized["task_invocation"] is not None:
            # None is the real successful/idempotent after_call return value.
            usage.after_call(normalized["task_invocation"], normalized["provider_actual_micro"], normalized["total_tokens"])
        return normalized["actual_micro"] if normalized["source"] == "platform" else 0


def install_recovery(monkeypatch):
    from app.credits import gate
    fake = FakeRecovery()
    fake.original_reconcile = getattr(gate, "reconcile_receipt", None)
    monkeypatch.setattr(gate, "reconcile_receipt", fake.reconcile_receipt, raising=False)
    return fake

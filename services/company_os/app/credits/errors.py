"""Credit-metering errors. ``code`` is stable for HTTP mapping (402 / 503)."""

from __future__ import annotations

LIMIT_MESSAGE = "You have reached your usage limit. Buy more to continue."


class CreditError(Exception):
    """Base for every credit-layer failure."""

    code: str = "credit_error"


class InsufficientCredits(CreditError):
    """Reserve predicate failed. API renders 402 from the carried numbers."""

    code = "insufficient_credits"

    def __init__(
        self, account_id: str, available_micro: int, required_micro: int
    ) -> None:
        self.account_id = account_id
        self.available_micro = available_micro
        self.required_micro = required_micro
        super().__init__(
            f"account {account_id} has {available_micro} micro-credits "
            f"available, {required_micro} required"
        )


class CreditsUnavailable(CreditError):
    """Gate absent while required, or no account when fail-closed."""

    code = "credits_unavailable"

    def __init__(self, message: str = "credit metering is not available") -> None:
        super().__init__(message)


class AccountSuspended(CreditError):
    """Account is not ``active`` (suspended or frozen)."""

    code = "account_suspended"

    def __init__(self, account_id: str, status: str = "suspended") -> None:
        self.account_id = account_id
        self.status = status
        super().__init__(f"credit account {account_id} is {status}")


class DuplicateLedgerEntry(CreditError):
    """UNIQUE ``idempotency_key`` collision — caller should return the original row."""

    code = "duplicate_ledger_entry"

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"ledger idempotency key already applied: {idempotency_key}")

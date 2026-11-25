"""Workspace AI admission, including jobs that never pass through the edge."""

import hashlib
import json

from core.ratelimit import RateLimiter

from app.core.jobs import RetryableError
from app.core.local_worker import redis_url

limiter = RateLimiter(redis_url=redis_url())


def gmail_account_key(founder_id, connected_account_id):
    return hashlib.sha256(json.dumps([founder_id, connected_account_id]).encode()).hexdigest()


class RateLimitedJob(RetryableError):
    def __init__(self, seconds: int) -> None:
        super().__init__("rate_limited")
        self.retry_after_s = seconds


def check_workspace(workspace_id: str) -> None:
    limiter.check_ai(f"ai-workspace:{workspace_id}")


def check_job(kind: str) -> None:
    from app.tenancy import current

    if kind in {"gtm.journey.maintain", "gtm.gmail.read", "gtm.outreach.retry"}:
        return
    if kind.startswith(("idea.", "research.", "plan.", "content.", "build.", "app.", "import.", "pagebuilder.", "simulation.", "gtm.")) or kind == "task.run":
        if kind in {"build.watch", "import.cleanup"}:
            return  # 30s build polling / cleanup would drain the daily AI cap without generating anything.
        scope = current()
        check_workspace(scope.workspace_id)

"""Live publishers, one per platform.

Each returns a PublishResult and never raises for a platform-side refusal —
a 403 is information the learning loop needs, not a crash.
"""

from app.content.platforms.base import PublishResult, dry_run_enabled
from app.content.platforms.linkedin import publish_linkedin
from app.content.platforms.reddit import publish_reddit
from app.content.platforms.x import publish_x

PUBLISHERS = {
    "linkedin": publish_linkedin,
    "reddit": publish_reddit,
    "x": publish_x,
}

__all__ = [
    "PUBLISHERS",
    "PublishResult",
    "dry_run_enabled",
    "publish_linkedin",
    "publish_reddit",
    "publish_x",
]

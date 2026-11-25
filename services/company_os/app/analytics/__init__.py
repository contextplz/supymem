"""Product analytics (doc 112). First-party only — no third-party SaaS."""

from app.analytics.events import emit
from app.analytics.rollup import latest_product_metrics, rollup_today

__all__ = ["emit", "latest_product_metrics", "rollup_today"]

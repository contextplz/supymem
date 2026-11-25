from connectors.channels.email import EmailAdapter
from connectors.channels.instagram import InstagramAdapter
from connectors.channels.landing import LandingAdapter
from connectors.channels.linkedin import LinkedInAdapter
from connectors.channels.reddit import RedditAdapter
from connectors.channels.x import XAdapter

ADAPTERS = {
    "landing": LandingAdapter,
    "email": EmailAdapter,
    "x": XAdapter,
    "linkedin": LinkedInAdapter,
    "reddit": RedditAdapter,
    "instagram": InstagramAdapter,
}

__all__ = ["ADAPTERS", "LandingAdapter", "EmailAdapter", "XAdapter", "LinkedInAdapter", "RedditAdapter", "InstagramAdapter"]

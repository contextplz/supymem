"""Hard gates for real side effects when SANDBOX_MODE is on (PS-4)."""

from __future__ import annotations

from app.ops import config as ops_config

CHANNEL_TWITTER = "twitter"
CHANNEL_SENDGRID = "sendgrid"
CHANNEL_STRIPE_WRITE = "stripe_write"
CHANNEL_ADS_WRITE = "ads_write"
CHANNEL_RENDER = "render"


class SandboxBlocked(RuntimeError):
    """Raised when a real write is attempted under SANDBOX_MODE."""


def assert_write_allowed(channel: str) -> None:
    if ops_config.sandbox_mode():
        raise SandboxBlocked(
            f"SANDBOX_MODE blocks real writes on channel={channel!r}. "
            "Set SANDBOX_MODE=false and provide credentials to proceed."
        )


def assert_twitter_write() -> None:
    assert_write_allowed(CHANNEL_TWITTER)


def assert_sendgrid_write() -> None:
    assert_write_allowed(CHANNEL_SENDGRID)


def assert_stripe_write() -> None:
    assert_write_allowed(CHANNEL_STRIPE_WRITE)


def assert_ads_write() -> None:
    assert_write_allowed(CHANNEL_ADS_WRITE)


def assert_render_write() -> None:
    assert_write_allowed(CHANNEL_RENDER)

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.channels.email import EmailAdapter  # noqa: E402
from app.gtm.channels.instagram import InstagramAdapter  # noqa: E402
from app.gtm.channels.landing import LandingAdapter  # noqa: E402
from app.gtm.channels.linkedin import LinkedInAdapter  # noqa: E402
from app.gtm.channels.reddit import RedditAdapter  # noqa: E402
from app.gtm.channels.x import XAdapter  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type",
    [XAdapter, LinkedInAdapter, RedditAdapter, InstagramAdapter, EmailAdapter, LandingAdapter],
)
async def test_unimplemented_channel_never_claims_live(
    adapter_type: type[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    adapter = adapter_type(dry_run=False)  # type: ignore[call-arg]

    result = await adapter.publish({"text": "test"}, warden_allow=True)  # type: ignore[attr-defined]

    assert result["mode"] == "simulated"
    assert result["external_ref"] is None
    assert "live" not in result

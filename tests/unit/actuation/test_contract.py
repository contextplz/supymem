from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.actuation.contract import ActuationMode, ActuationResult, assert_live_is_honest  # noqa: E402


def test_live_without_external_ref_fails_honesty_assertion() -> None:
    result = ActuationResult(
        mode=ActuationMode.LIVE,
        tool_id="social.post",
        venture_id="ven_test",
        ok=True,
    )

    with pytest.raises(ValueError, match="external_ref"):
        assert_live_is_honest(result)


def test_live_with_external_ref_passes_honesty_assertion() -> None:
    result = ActuationResult(
        mode=ActuationMode.LIVE,
        tool_id="social.post",
        venture_id="ven_test",
        ok=True,
        external_ref="post_123",
    )

    assert_live_is_honest(result)

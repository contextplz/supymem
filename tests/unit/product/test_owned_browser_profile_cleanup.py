"""Closing an owned browser removes only its own temporary profile."""
from types import SimpleNamespace

import pytest

from coding_agents.owned_browser import OwnedBrowser


def browser(**kwargs):
    return OwnedBrowser(order_id="dwo_profile_cleanup", goal="Read the headline",
        origins=["https://example.invalid"], authorize=lambda *_: True,
        admit_effect=lambda *_: False, **kwargs)


@pytest.mark.parametrize("close_fails", [False, True])
def test_private_fallback_profile_removed_even_when_browser_close_fails(close_fails):
    owned = browser()
    profile = owned._profile_dir
    assert profile.is_dir()
    (profile / "owned-fixture-data").write_text("fixture")

    def close():
        if close_fails:
            raise RuntimeError("fixture_close_failed")

    owned._browser = SimpleNamespace(close=close)
    if close_fails:
        with pytest.raises(RuntimeError, match="fixture_close_failed"):
            owned.close()
    else:
        owned.close()
    assert not profile.exists()
    owned.close()


def test_caller_owned_profile_preserved(tmp_path):
    profile = tmp_path / "caller-profile"
    owned = browser(profile_dir=profile)
    sentinel = profile / "caller-fixture-data"
    sentinel.write_text("caller-owned")
    owned.close()
    assert sentinel.read_text() == "caller-owned"


def test_account_factory_profile_preserved(tmp_path):
    profile = tmp_path / "account-profile"
    profile.mkdir()
    sentinel = profile / "account-fixture-data"
    sentinel.write_text("account-owned")
    stops = []
    factory = SimpleNamespace(headless=True, stop=lambda: stops.append(True))
    owned = browser(chrome_factory=factory)
    assert owned._profile_dir is None
    owned.close()
    assert stops == [True] and sentinel.read_text() == "account-owned"

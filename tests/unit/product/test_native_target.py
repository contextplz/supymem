"""Inventory boundary regressions, not native desktop execution acceptance."""
import pytest
from coding_agents.native_target import NativeTarget, TargetUnavailable, resolve_target

BUNDLE = "com.example.Editor"


def test_exact_bundle_identity_ignores_similarly_named_frontmost_decoy():
    apps = [{"pid": 11, "bundle_id": BUNDLE}, {"pid": 22, "bundle_id": BUNDLE + "Preview", "name": BUNDLE}]
    windows = [{"pid": 22, "window_id": 101, "z_index": 999, "title": BUNDLE},
               {"pid": 11, "window_id": 102, "app_name": "Localized editor"}]
    assert resolve_target(BUNDLE, apps, windows) == NativeTarget(BUNDLE, 11, 102)
    with pytest.raises(TargetUnavailable, match="application_unverified"):
        resolve_target(BUNDLE, apps[1:], windows)


def test_multiple_windows_need_exact_prior_binding_and_never_retarget_after_disappearance():
    apps = [{"pid": 11, "bundleId": BUNDLE}]
    windows = [{"pid": 11, "window_id": 101}, {"pid": 11, "window_id": 102}]
    with pytest.raises(TargetUnavailable, match="ambiguous"):
        resolve_target(BUNDLE, apps, windows)
    prior = NativeTarget(BUNDLE, 11, 101)
    assert resolve_target(BUNDLE, apps, windows, previous=prior) == prior
    with pytest.raises(TargetUnavailable, match="binding_stale"):
        resolve_target(BUNDLE, apps, windows[1:], previous=prior)


@pytest.mark.parametrize("apps,windows", [
    ([{"pid": True, "bundle_id": BUNDLE}], [{"pid": True, "window_id": 1}]),
    ([{"pid": "11", "bundle_id": BUNDLE}], [{"pid": 11, "window_id": 1}]),
    ([{"pid": 11, "name": BUNDLE}], [{"pid": 11, "window_id": 1}]),
    ([{"pid": 11, "bundle_id": BUNDLE, "running": False}], [{"pid": 11, "window_id": 1}]),
    ([{"pid": 11, "bundle_id": BUNDLE}, {"pid": 11, "bundle_id": "com.other.App"}], [{"pid": 11, "window_id": 1}]),
    ([{"pid": 11, "bundle_id": BUNDLE}], [{"pid": 11, "window_id": True}]),
    ([{"pid": 11, "bundle_id": BUNDLE}], [{"pid": 11, "window_id": 1, "bundle_id": "com.other.App"}]),
])
def test_unverified_or_conflicting_inventory_never_becomes_a_target(apps, windows):
    with pytest.raises(TargetUnavailable):
        resolve_target(BUNDLE, apps, windows)

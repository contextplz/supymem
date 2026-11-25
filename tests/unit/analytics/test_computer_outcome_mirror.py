"""The server's computer_outcome must match the Mac runtime's outcome_code without importing it."""
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True)
def _app():
    load_company_os_app()

REASONS = ["timeout", "cu_wall_timeout", "ai_loop_time_limit", "auth_lost", "cu_session_expired", "desktop_auth_required",
           "model_unavailable", "cu_browser_decision_unavailable", "blocked", "origin_violation_stopped",
           "host_permissions_missing", None, "", "cancelled", "something_new", "verified"]


@pytest.mark.parametrize("verified", [True, False, None])
@pytest.mark.parametrize("reason", REASONS)
def test_server_outcome_matches_runtime(verified, reason):
    from desktop_runtime import cu_reliability
    from app.analytics import observability

    assert observability.computer_outcome(verified=verified, reason=reason) == cu_reliability.outcome_code(verified=verified, reason=reason)


def test_every_runtime_code_is_mirrored():
    from desktop_runtime import cu_reliability
    from app.analytics import observability

    mirrored = observability._TIMEOUT | observability._AUTH_LOST | observability._MODEL | observability._BLOCKED
    for code in mirrored:
        assert observability.computer_outcome(verified=False, reason=code) == cu_reliability.outcome_code(verified=False, reason=code)


def test_server_module_never_imports_the_desktop_runtime():
    from app.analytics import observability

    assert "from desktop_runtime" not in open(observability.__file__).read()

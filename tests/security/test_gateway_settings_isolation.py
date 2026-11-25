import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("preceding", [
    "tests/security/test_admin_boundary.py::test_admin_login_requires_totp",
    "tests/security/test_gateway_retry_boundary.py::test_transient_failure_is_not_cached",
    "tests/security/test_gateway_users_engine_pool.py::test_pg_engine_is_built_once_per_url",
    "tests/integration/test_gateway_auth.py::test_auth_session_unauthenticated",
])
def test_gateway_settings_do_not_leak_into_telemetry(preceding):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", preceding,
         "tests/security/test_observability_telemetry.py"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "COS_TEST_APP_REUSE": "1"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""SANDBOX_MODE hard gate (doc 49 PS-4)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_sb.db")
    load_company_os_app()


def test_sandbox_blocks_writes_by_default(ops_env, monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_MODE", "true")
    from app.ops.sandbox import SandboxBlocked, assert_twitter_write, assert_render_write

    with pytest.raises(SandboxBlocked):
        assert_twitter_write()
    with pytest.raises(SandboxBlocked):
        assert_render_write()


def test_sandbox_allows_when_disabled(ops_env, monkeypatch) -> None:
    monkeypatch.setenv("SANDBOX_MODE", "false")
    from app.ops.sandbox import assert_stripe_write

    assert_stripe_write()  # does not raise

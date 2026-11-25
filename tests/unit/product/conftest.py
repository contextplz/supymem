import pytest


@pytest.fixture(autouse=True)
def legacy_cu_engine_by_default(monkeypatch):
    """These suites pin the gated single-window engine. The AI loop has its own tests (test_cu_ai_*.py);
    opt in per test with monkeypatch.setenv("SUPYMEM_CU_ENGINE", "ai_loop")."""
    monkeypatch.setenv("SUPYMEM_CU_ENGINE", "legacy")

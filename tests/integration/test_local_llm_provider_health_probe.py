from __future__ import annotations

from app.llm import founder_provider


def test_local_provider_defaults_base_url(tmp_path, monkeypatch):
    # Use an isolated DB via existing test fixtures if present; otherwise skip persistence
    # and only validate validation helpers.
    assert "local" in founder_provider.SUPPORTED_PROVIDERS
    # _clean_url accepts localhost http
    url = founder_provider._clean_url("http://localhost:11434/v1")
    assert url == "http://localhost:11434/v1"

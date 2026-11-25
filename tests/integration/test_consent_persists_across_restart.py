from __future__ import annotations

import importlib

from coding_agents import consent


def test_consent_persists_across_module_reload(tmp_path, monkeypatch):
    db = tmp_path / "consent.sqlite3"
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(db))
    consent.clear_consents()
    consent.record_consent("cursor", version="2026.1")
    assert consent.has_consent("cursor")

    # Simulate process restart: clear memory, reload module logic via clear + new has_consent.
    consent._MEMORY.clear()
    importlib.reload(consent)
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(db))
    assert consent.has_consent("cursor") is True


def test_platform_adapters_skip_consent(monkeypatch, tmp_path):
    monkeypatch.setenv("CODING_AGENT_CONSENT_DB", str(tmp_path / "c.sqlite3"))
    from coding_agents.consent import clear_consents, require_consent

    clear_consents()
    require_consent("inhouse")
    require_consent("openhands_builder")

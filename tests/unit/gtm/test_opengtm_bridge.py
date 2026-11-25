"""OpenGTM bridge: normalize leads + status without calling Gemini."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm import opengtm_bridge


@pytest.mark.parametrize("operation", ["load", "status", "queue"])
@pytest.mark.parametrize("state_file", ["/tmp/outside.json", "../outside.json", "nested/file.json", "nested\\file.json", "", "a" * 65 + ".json", "state.txt"])
def test_outreach_rejects_unsafe_state_file(monkeypatch, tmp_path, operation, state_file):
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", tmp_path / "home")
    monkeypatch.setattr(opengtm_bridge, "_require_bin", lambda: None)
    monkeypatch.setattr(opengtm_bridge, "_run", lambda *args, **kwargs: pytest.fail("unsafe state path reached CLI"))
    function = getattr(opengtm_bridge, "outreach_" + operation)
    with pytest.raises(ValueError):
        function([], state_file=state_file) if operation == "load" else function(state_file=state_file)


@pytest.mark.parametrize("operation", ["load", "status", "queue"])
def test_outreach_rejects_state_symlink_escape(monkeypatch, tmp_path, operation):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (home / "safe.json").symlink_to(outside)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", home)
    monkeypatch.setattr(opengtm_bridge, "_require_bin", lambda: None)
    monkeypatch.setattr(opengtm_bridge, "_run", lambda *args, **kwargs: pytest.fail("symlink escape reached CLI"))
    function = getattr(opengtm_bridge, "outreach_" + operation)
    with pytest.raises(ValueError):
        function([], state_file="safe.json") if operation == "load" else function(state_file="safe.json")


@pytest.mark.parametrize("operation", ["load", "status", "queue"])
def test_outreach_requires_binary_before_state_filesystem(monkeypatch, tmp_path, operation):
    home = tmp_path / "home"
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", home)

    def unavailable():
        raise RuntimeError("not installed")

    monkeypatch.setattr(opengtm_bridge, "_require_bin", unavailable)
    monkeypatch.setattr(opengtm_bridge.Path, "is_file", lambda *args: pytest.fail("state filesystem probed before binary check"))
    function = getattr(opengtm_bridge, "outreach_" + operation)
    with pytest.raises(RuntimeError, match="not installed"):
        function([]) if operation == "load" else function()
    assert not home.exists()


@pytest.mark.parametrize("state_file", [None, "campaign_1.json"])
def test_outreach_state_basename_load_status_queue(monkeypatch, tmp_path, state_file):
    from pathlib import Path
    from subprocess import CompletedProcess

    home = tmp_path / "home"
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", home)
    monkeypatch.setattr(opengtm_bridge, "_require_bin", lambda: None)
    calls = []

    def run(args, **kwargs):
        path = Path(args[args.index("--state-file") + 1])
        assert path == home / (state_file or "outreach-state.json")
        calls.append(args[1])
        if args[1] == "load":
            path.write_text("{}")
        return CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(opengtm_bridge, "_run", run)
    opengtm_bridge.outreach_load([], state_file=state_file)
    opengtm_bridge.outreach_status(state_file=state_file)
    opengtm_bridge.outreach_queue(state_file=state_file)
    assert calls == ["load", "status", "status", "queue", "status"]


def test_normalize_lead_from_pipeline_row():
    row = {
        "company": "Acme Analytics",
        "domain": "acme.example",
        "industry": "B2B SaaS",
        "region": "Berlin",
        "contact_name": "Ada Lovelace",
        "contact_title": "Founder",
        "contact_email": "ada@acme.example",
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
        "qualification": {"score": 82, "tier": "hot"},
        "messages": {"pattern": "A", "connection_request": "Hi Ada…"},
        "audit": {"findings": [{"type": "meta_description_missing"}]},
    }
    lead = opengtm_bridge.normalize_lead(row)
    assert lead["first_name"] == "Ada"
    assert lead["last_name"] == "Lovelace"
    assert lead["email"] == "ada@acme.example"
    assert lead["organization"] == "Acme Analytics"
    assert lead["website"] == "https://acme.example"
    assert lead["source"] == "opengtm"
    assert lead["icp_score"] == 82
    assert lead["icp_tier"] == "hot"
    assert lead["outreach_pattern"] == "A"
    assert lead["city"] == "Berlin"


def test_normalize_leads_skips_non_dicts():
    assert opengtm_bridge.normalize_leads(
        [{"company": "X", "domain": "x.com"}, "bad"]  # type: ignore[list-item]
    ) == [
        opengtm_bridge.normalize_lead({"company": "X", "domain": "x.com"})
    ]


def test_message_input_normalizes_company_os_audit_fields():
    row = opengtm_bridge._message_ready_lead(
        {
            "company": "Acme",
            "audit": {
                "findings": [
                    {
                        "category": "positioning",
                        "severity": "high",
                        "finding": "No role-specific use case",
                        "recommendation": "Add one",
                    }
                ]
            },
        }
    )
    assert row["audit"]["findings"] == [
        {
            "category": "positioning",
            "severity": "high",
            "finding": "No role-specific use case",
            "recommendation": "Add one",
            "type": "positioning",
            "detail": "No role-specific use case",
            "evidence": "No role-specific use case",
        }
    ]


def test_status_reports_bin_and_gemini(monkeypatch, tmp_path):
    fake_bin = tmp_path / "opengtm"
    fake_bin.write_text("#!/bin/sh\necho help\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_BIN", fake_bin)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with patch.object(opengtm_bridge.subprocess, "run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "usage: opengtm"
        run.return_value.stderr = ""
        st = opengtm_bridge.status()
    assert st["ready"] is True
    assert st["gemini"]["ready"] is False
    assert st["gemini"]["code"] == "founder_scoped_account_required"
    assert st["crm_webhook"]["ready"] is False
    assert st["offline_ok"] is True


def test_qualify_invokes_cli_with_temp_json(monkeypatch, tmp_path):
    fake_bin = tmp_path / "opengtm"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_BIN", fake_bin)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_DIR", tmp_path)
    monkeypatch.setattr(opengtm_bridge, "OPENGTM_HOME", tmp_path / "home")

    qualified = [
        {
            "company": "Acme",
            "domain": "acme.example",
            "industry": "B2B SaaS",
            "qualification": {"score": 70, "tier": "warm"},
            "audit": {"findings": []},
        }
    ]

    def fake_run(cmd, **_kwargs):
        # Last --output path gets the fixture
        out = None
        for i, arg in enumerate(cmd):
            if arg == "--output" and i + 1 < len(cmd):
                out = cmd[i + 1]
        assert out
        import json
        from pathlib import Path

        Path(out).write_text(json.dumps(qualified), encoding="utf-8")

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return R()

    monkeypatch.setattr(opengtm_bridge.subprocess, "run", fake_run)
    # status() also calls subprocess — stub ready
    monkeypatch.setattr(
        opengtm_bridge,
        "status",
        lambda: {"ready": True, "gemini": {"ready": False}},
    )

    result = opengtm_bridge.qualify(
        [{"organization": "Acme", "domain": "acme.example", "industry": "B2B SaaS"}],
        icp_profile="saas",
    )
    assert result["count"] == 1
    assert result["warm"] == 1
    assert result["leads"][0]["icp_tier"] == "warm"
    assert result["leads"][0]["source"] == "opengtm"

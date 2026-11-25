from __future__ import annotations

from coding_agents.env import (
    child_env,
    load_dotenv_file,
    mark_company_os_owned,
    set_company_os_config,
)


def test_llm_api_key_scrubbed_when_marked():
    mark_company_os_owned(["LLM_API_KEY"])
    base = {"LLM_API_KEY": "secret", "PATH": "/usr/bin", "HOME": "/tmp"}
    env = child_env(base)
    assert "LLM_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_separate_config_dict_scrubs_without_parent_merge(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=platform-secret\nPATH=/opt/bin\n", encoding="utf-8")
    cfg = load_dotenv_file(env_file)
    set_company_os_config(cfg, mark_auth=True)
    # Founder shell still has a different key — we scrub the platform-owned name.
    base = {"LLM_API_KEY": "platform-secret", "PATH": "/usr/bin", "HOME": "/tmp"}
    env = child_env(base)
    assert "LLM_API_KEY" not in env

from __future__ import annotations

import os

from coding_agents.env import child_env, mark_company_os_owned


def test_llm_api_key_scrubbed():
    mark_company_os_owned(["LLM_API_KEY"])
    base = {"LLM_API_KEY": "secret", "PATH": "/usr/bin", "HOME": "/tmp"}
    env = child_env(base)
    assert "LLM_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"

"""Always-on social agent — not a stagnant connector.

Loop: keep-alive → sense → decide → act → remember.
Runs on the Will scheduler so it lives as long as Company OS does.
"""

from __future__ import annotations

from app.content.agent.loop import agent_status, run_agent_tick

__all__ = ["run_agent_tick", "agent_status"]

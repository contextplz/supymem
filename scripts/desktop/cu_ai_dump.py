"""Dev helper: print what the AI loop's cua backend sees for an app (no actions).

Usage: cu_ai_dump.py TextEdit [menus]   (needs the trial daemon: see cu_ai_trial.py)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from coding_agents.cu_ai_loop import element_lines  # noqa: E402
from desktop_runtime.cu_ai_cua import CuaMac, session_caller  # noqa: E402
from desktop_runtime.cua_session import AI_LOOP_TOOLS, CuaSession  # noqa: E402

binary = os.environ.get("CU_CUA", "/Applications/Supymem.app/Contents/MacOS/cua-driver")
mac = CuaMac(session_caller(CuaSession(binary, "/private/tmp/supymem-cua-trial/driver.sock", tools=AI_LOOP_TOOLS)))
print(mac.switch_app(sys.argv[1]))
obs = mac.observe()
print("window:", obs.window_title, "| shot", mac.shot_window, "| ax", mac.ax_window, "| png", len(obs.png),
      "| raw", len(mac.raw))
if len(sys.argv) > 2:
    for e in mac.raw:
        if e.get("role") in ("AXMenuBarItem", "AXMenuItem", "AXMenu"):
            print(e["element_index"], e.get("parent_index"), e["role"], repr(e.get("label")))
else:
    print("\n".join(element_lines(obs.elements, 60)))
open("/tmp/cu_dump.png", "wb").write(obs.png)

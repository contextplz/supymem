"""Send a few synthetic cursor events to the dev overlay (see cu_cursor_up.sh) so a human can watch it glide."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages"))
from desktop_runtime import agent_cursor as c  # noqa: E402

with c.execution():
    for kind, x, y, label in (("click", 400, 300, "Clicking Save"), ("type", 900, 500, "Typing"), ("scroll", 600, 650, "Scrolling")):
        print(kind, c.emit(kind, x, y, label), flush=True)
        time.sleep(1.5)
    c.emit("done")

"""Latest per-step browser frame for the console watch panel (in memory only, never persisted or sent to Core).

The bridge status route merges `snapshot()` into the receipt while a browser task runs. The Tauri status
reply is capped at 256 KiB, so oversized frames are dropped. While a Needs-you card is open the feed is
paused: the frame is discarded and nothing new is captured (the founder may be typing a password).
"""
from __future__ import annotations

import base64
import threading
import time

MAX_FRAME_B64 = 150_000


class WatchFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0
        self.paused = False

    def publish(self, jpeg: bytes, *, step: int, label: str = "", cursor: dict | None = None,
                viewport: dict | None = None) -> bool:
        encoded = base64.b64encode(jpeg).decode("ascii") if jpeg else ""
        with self._lock:
            if self.paused or not encoded or len(encoded) > MAX_FRAME_B64:
                return False
            self._seq += 1
            self._frame = {"seq": self._seq, "step": step, "at": time.time(), "jpeg_b64": encoded,
                           "label": str(label)[:40], "cursor": cursor, "viewport": viewport}
            return True

    def pause(self) -> None:
        with self._lock:
            self.paused, self._frame = True, None

    def resume(self) -> None:
        with self._lock:
            self.paused = False

    def clear(self) -> None:
        with self._lock:
            self._frame = None

    def snapshot(self) -> dict | None:
        with self._lock:
            return None if self.paused else self._frame

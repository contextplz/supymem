from __future__ import annotations

import builtins
from pathlib import Path

from coding_agents.detect import detect

FORBIDDEN = [Path.home() / ".claude", Path.home() / ".cursor", Path.home() / ".codex"]


def test_detect_does_not_touch_credential_dirs(tmp_path, monkeypatch):
    opened = []
    real_open = builtins.open

    def spy_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    p = bin_dir / "claude"
    p.write_text("#!/bin/sh\necho 1\n")
    p.chmod(0o755)
    detect(path_env=str(bin_dir))
    for f in FORBIDDEN:
        assert not any(str(f) in o for o in opened)

"""Fail if GitHub workflow YAML uses `shell: true` (command injection footgun)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEEDLE = re.compile(r"^\s+shell:\s*true\s*$", re.M)


def main() -> int:
    bad: list[str] = []
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if NEEDLE.match(line) and not line.strip().startswith("#"):
                bad.append(f"{path.relative_to(ROOT)}:{i}")
    if bad:
        print("shell: true is not allowed in GitHub workflows:", file=sys.stderr)
        for row in bad:
            print(f"  {row}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

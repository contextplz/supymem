#!/usr/bin/env python3
"""Record a product-demo MP4 (Playwright + ffmpeg) or ingest a Recordly export.

Does not publish. CONTENT_DRY_RUN / marketing floors still apply to posts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "company_os"))
sys.path.insert(0, str(ROOT / "packages"))


def _load_env() -> None:
    for name in (".env", ".env.local"):
        path = ROOT / name
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = val.strip().strip("'").strip('"')


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Record or ingest a product demo video")
    parser.add_argument("--url", default=os.environ.get("RECORD_DEMO_URL") or "http://127.0.0.1:3100/content")
    parser.add_argument("--headline", default="Product demo")
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--headed", action="store_true", help="Show Chromium while recording")
    parser.add_argument("--ingest", default="", help="Path to a Recordly-exported MP4/GIF")
    parser.add_argument("--launch-recordly", action="store_true", help="Open Recordly.app (human records)")
    parser.add_argument("--no-short", action="store_true", help="Skip 9:16 short clip")
    parser.add_argument("--workspace", default="")
    args = parser.parse_args()

    from app.marketing.media.demo_recorder import record_product_demo
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    workspace_id = (args.workspace or desk.id).strip()
    if args.headed:
        os.environ["RECORD_DEMO_HEADED"] = "1"

    out = record_product_demo(
        workspace_id=workspace_id,
        url=args.url,
        headline=args.headline,
        seconds=args.seconds,
        headed=bool(args.headed) or None,
        ingest_path=args.ingest.strip() or None,
        launch_recordly=bool(args.launch_recordly),
        short_clip=not args.no_short,
    )
    print(json.dumps(out, indent=2, default=str)[:8000])
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Fetch skill packs at the immutable refs committed in MANIFEST.yaml."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml


def fetch(manifest_path: Path) -> list[str]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    root = manifest_path.parent
    fetched: list[str] = []
    for pack in manifest.get("packs", []):
        destination = root / pack.get("dir", pack["id"])
        if destination.exists():
            continue
        destination.mkdir(parents=True)
        try:
            subprocess.run(["git", "-C", str(destination), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(destination), "fetch", "--depth", "1", pack["repo"], pack["ref"]],
                check=True,
            )
            subprocess.run(["git", "-C", str(destination), "checkout", "-q", "FETCH_HEAD"], check=True)
        except BaseException:
            # Do not leave a partially fetched pack looking successfully installed.
            shutil.rmtree(destination)
            raise
        fetched.append(str(pack["id"]))
    return fetched


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    try:
        print("\n".join(fetch(root / "vendor/skills/MANIFEST.yaml")))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc

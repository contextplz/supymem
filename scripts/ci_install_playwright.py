#!/usr/bin/env python3
"""Provision the full Firefox renderer on Ubuntu Actions runners.

Runner mirror+file sources keep their suites, components and Signed-By unchanged.
https://manpages.ubuntu.com/manpages/noble/man1/apt-transport-mirror.1.html
https://manpages.ubuntu.com/manpages/noble/man5/apt.conf.5.html
"""

import os
from pathlib import Path
import platform
import subprocess
import sys
from urllib.parse import urlsplit


APT_NETWORK_CONFIG = '''Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
Acquire::Retries "2";
'''
ARCHIVE = "https://archive.ubuntu.com/ubuntu/"
SECURITY = "https://security.ubuntu.com/ubuntu/"


def configure_apt(apt_root: Path) -> None:
    for mirror in apt_root.glob("apt-mirrors*.txt"):
        lines = []
        removed = False
        for line in mirror.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                uri = line.split()[0]
                parsed = urlsplit(uri)
                if parsed.hostname == "azure.archive.ubuntu.com" and parsed.path.rstrip("/") == "/ubuntu":
                    removed = True
                    continue
                if parsed.hostname in {"archive.ubuntu.com", "security.ubuntu.com"} and parsed.scheme == "http":
                    line = line.replace(uri, "https:" + uri[len("http:"):], 1)
            lines.append(line)
        if removed:
            official = (SECURITY, ARCHIVE) if "security" in mirror.name else (ARCHIVE, SECURITY)
            for priority, uri in enumerate(official, start=1):
                if not any(line.split() and line.split()[0].rstrip("/") == uri.rstrip("/") for line in lines):
                    lines.append(f"{uri}\tpriority:{priority}")
            mirror.write_text("\n".join(lines) + "\n")
    config = apt_root / "apt.conf.d" / "99supymem-ci-network"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(APT_NETWORK_CONFIG)


def main(configure_only: bool = False) -> None:
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or platform.freedesktop_os_release().get("ID") != "ubuntu"):
        raise RuntimeError("Playwright CI provisioning requires an Ubuntu GitHub Actions runner")
    if configure_only:
        configure_apt(Path("/etc/apt"))
        return
    subprocess.run(["sudo", "--preserve-env=GITHUB_ACTIONS,RUNNER_ENVIRONMENT", "-n", sys.executable,
                    str(Path(__file__).resolve()), "--configure-apt"], check=True, timeout=30)
    environment = {**os.environ, "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "120000"}
    subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "firefox"],
                   check=True, env=environment, timeout=480)


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--configure-apt"]):
        raise SystemExit("usage: ci_install_playwright.py [--configure-apt]")
    main(configure_only=bool(sys.argv[1:]))

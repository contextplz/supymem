"""Render BuilderChoiceCard in Chromium with mocked, credential-free Mac detection.

Run from apps/console after `pnpm install`: python scripts/check_provider_builder_controls.py
Requires Python Playwright and local Chrome. The temporary Next route is removed afterward.
"""

import asyncio
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "src/app/provider-connection-smoke"
PAGE = '''"use client";
import { useState } from "react";
import { BuilderChoiceCard } from "@/components/onboarding/BuilderChoiceCard";
export default function Smoke() {
  const [chosen, setChosen] = useState("");
  return <main><div data-testid="chosen">{chosen}</div><BuilderChoiceCard ventureId="smoke" phase="choice" onChoose={async (choice) => setChosen(choice)} onSessionDone={async () => {}} submitting={false} /></main>;
}
'''
TOOLS = [
    dict(name="cursor", label="Cursor", kind="ide", installed=True, connected=False,
         app_path="/Applications/Cursor.app", cli_path=None),
    dict(name="claude", label="Claude Code", kind="ide", installed=True, connected=False,
         app_path="/Applications/Claude.app", cli_path=None),
    dict(name="codex", label="Codex", kind="ide", installed=True, connected=False,
         app_path="/Applications/Codex.app", cli_path=None),
]


async def check(url):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        for available in (False, True):
            tools = [dict(tool, cli_path=f"/usr/local/bin/{tool['name']}") for tool in TOOLS] if available else TOOLS
            page = await browser.new_page()
            await page.add_init_script(
                f'window.__TAURI_INTERNALS__ = {{ invoke: async (cmd) => cmd === "list_local_tools" ? {json.dumps(tools)} : null }};'
            )
            await page.goto(url)
            codex = page.locator('button:has(strong:text-is("Codex"))')
            claude = page.locator('button:has(strong:text-is("Claude Code"))')
            await codex.wait_for()
            await page.get_by_role("button", name="Re-detect local tools").wait_for()
            await codex.get_by_text(
                "Launch path found" if available else "Install the Codex CLI", exact=False
            ).wait_for()
            assert await codex.is_enabled() == available
            assert await claude.is_enabled() == available
            if available:
                await codex.click()
                assert await page.get_by_test_id("chosen").inner_text() == "codex"
            else:
                assert "Install the Codex CLI" in await codex.inner_text()
                assert await page.get_by_test_id("chosen").inner_text() == ""
            await page.close()
        await browser.close()


def main():
    if ROUTE.exists():
        raise RuntimeError(f"Temporary smoke route already exists: {ROUTE}")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ROUTE.mkdir()
    (ROUTE / "page.tsx").write_text(PAGE)
    process = subprocess.Popen(["pnpm", "exec", "next", "dev", "-p", str(port), "-H", "127.0.0.1"],
                               cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/provider-connection-smoke"
    try:
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError("Next dev server exited before the browser check")
            try:
                urllib.request.urlopen(url, timeout=1).close()
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Next dev server did not become ready")
        asyncio.run(check(url))
        print("Rendered provider builder controls: app-only disabled, CLI-present enabled and clickable")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        (ROUTE / "page.tsx").unlink()
        ROUTE.rmdir()


if __name__ == "__main__":
    main()

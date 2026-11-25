"""Click the existing-company onboarding choices in a local, credential-free browser."""

import asyncio
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "src/app/company-path-smoke"
PAGE = '''"use client";
import { useState } from "react";
import { CompanyPath } from "@/components/onboarding/CompanyPath";
export default function Smoke() {
  const [result, setResult] = useState(null);
  return <main><div data-testid="result">{result ? JSON.stringify(result) : ""}</div>
    <CompanyPath busy={false} error="" onBack={() => {}}
      onCommit={async (payload) => setResult(payload)} onError={() => {}} /></main>;
}
'''


async def check(url):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.add_init_script(
            'window.__TAURI_INTERNALS__ = { invoke: async () => null };'
        )
        await page.goto(url)
        try:
            await page.get_by_role("button", name="AI-native company").click(timeout=10000)
        except Exception:
            raise RuntimeError(f"Smoke route did not render: {(await page.locator('body').inner_text())[:1000]}")
        await page.get_by_role("button", name="Claude", exact=True).click()
        await page.get_by_role("heading", name="What do you already use?").wait_for()
        await page.get_by_role("button", name="ChatGPT", exact=True).click()
        await page.get_by_role("button", name="Continue").click()
        await page.get_by_role("heading", name="What should we automate?").wait_for()
        await page.get_by_role("button", name="Research").click()
        await page.get_by_role("heading", name="What should we automate?").wait_for()
        await page.get_by_role("button", name="Engineering").click()
        await page.get_by_role("button", name="Continue").click()
        await page.get_by_role("heading", name="How far?").wait_for()
        await page.get_by_role("button", name="Suggest only").click()
        result = json.loads(await page.get_by_test_id("result").inner_text())
        assert result["answers"]["tools"] == ["claude", "chatgpt"]
        assert result["answers"]["automate"] == ["research", "engineering"]
        assert result["answers"]["how_far"] == "suggest"
        await browser.close()


def main():
    if ROUTE.exists():
        raise RuntimeError(f"Temporary smoke route already exists: {ROUTE}")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    ROUTE.mkdir()
    (ROUTE / "page.tsx").write_text(PAGE)
    process = subprocess.Popen(
        ["pnpm", "exec", "next", "dev", "-p", str(port), "-H", "127.0.0.1"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}/company-path-smoke"
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
        print("Existing-company selection persists two tool and two automation choices")
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

"""Real Chromium checks of the lab UI, with explicit synthetic HTTP fixtures.

Run after starting the console's package.json dev command. This verifies UI
behavior and isolation; server/metering acceptance is tested separately.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def check(url: str, output: Path):
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Lab verification accepts loopback URLs only")
    output.mkdir(parents=True, exist_ok=True)
    origin = urlsplit(url).scheme + "://" + urlsplit(url).netloc
    calls = []
    polls = []
    exports = []
    requested_variants = [1]
    quote_fingerprint = "a" * 64
    fixture = "<!doctype html><html lang='en'><title>UI fixture</title><body><main><h1>Synthetic preview fixture</h1><p>This fixture tests isolation, not generator quality.</p><a href='https://example.invalid/public/waitlists/ven_synthetic'>Join the waitlist</a></main></body></html>"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True)
        try:
            for authenticated in (False, True):
                context = browser.new_context(viewport={"width": 1440, "height": 1000})

                def gateway(route):
                    request = route.request
                    path = urlsplit(request.url).path
                    headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Allow-Headers": "Content-Type,Origin,X-CSRF-Token", "Access-Control-Allow-Methods": "GET,POST,OPTIONS"}
                    if request.method == "OPTIONS":
                        route.fulfill(status=204, headers=headers)
                        return
                    if path == "/admin/session":
                        data = {"authenticated": authenticated, "role": "super_admin", "email": "operator@example.invalid", "csrf": "synthetic-admin-csrf"}
                    elif path == "/auth/session":
                        data = {"authenticated": True, "subject": "synthetic-founder", "csrf": "synthetic-founder-csrf"}
                    elif path.endswith("/estimate"):
                        calls.append((path, request.post_data_json))
                        data = {"credits": 5.25, "seconds": 90, "ready": True, "quote_fingerprint": quote_fingerprint}
                    elif path.endswith("/start"):
                        calls.append((path, request.post_data_json))
                        requested_variants[0] = request.post_data_json["variants"]
                        data = {"job_id": "job_synthetic", "state": "queued", "variant_count": 1}
                        if "/ven_failed/" in path:
                            data = {"job_id": "job_stopped", "state": "failed", "error": "pagebuilder_generation_failed",
                                "receipt": {"cost_usd": 0.019, "unresolved_ceiling_usd": 0.015}}
                    elif path.endswith("/result"):
                        data = {"variant_count": requested_variants[0], "publish_ready": False, "receipt": {"cost_usd": 0, "credits": 0}, "manifest": {"fixture": True}}
                    elif path.endswith("/spec"):
                        exports.append(request.url)
                        data = {"markdown": "Synthetic selected-direction coding spec"}
                    elif path.endswith("/preview"):
                        route.fulfill(status=200, body=fixture, content_type="text/html", headers=headers)
                        return
                    elif path.endswith("/jobs/job_synthetic"):
                        polls.append(path)
                        data = {"job_id": "job_synthetic", "state": "running" if len(polls) == 1 else "ready", "stage": "render", "progress": 0.8 if len(polls) == 1 else 1}
                    else:
                        data = {}
                    route.fulfill(status=200, body=json.dumps(data), content_type="application/json", headers=headers)

                context.route("http://127.0.0.1:3200/**", gateway)
                # The existing root layout admits venture tools only in the Mac
                # shell. Exercise that supported path with a synthetic bridge;
                # this is not evidence that the web-portal allowlist was changed.
                context.add_init_script("""window.__TAURI__ = {
                    core: {invoke: async (command, args) => {
                        if(command==='gateway_url') return 'http://127.0.0.1:3200';
                        if(command==='desktop_auth_status') return {state:'connected'};
                        if(command==='desktop_api_request') {
                            const response=await fetch('http://127.0.0.1:3200'+args.path,{method:args.method,
                                body:args.body?JSON.stringify(args.body):undefined,
                                headers:args.body?{'Content-Type':'application/json'}:{}});
                            return {status:response.status,data:await response.json()};
                        }
                        if(command==='desktop_http_request') {
                            const body=args.body?.kind==='json'?JSON.stringify(args.body.value):undefined;
                            const response=await fetch('http://127.0.0.1:3200'+args.path,{method:args.method,body,
                                headers:body?{'Content-Type':'application/json'}:{}});
                            const text=await response.text();
                            return {status:response.status,bodyBase64:btoa(text),headers:{'content-type':response.headers.get('content-type')||'application/json'}};
                        }
                        return null;
                    }}, event: {listen:async()=>()=>{}}
                };""")
                page = context.new_page()
                page.goto(url.rstrip("/") + "/pagebuilder-lab")
                if not authenticated:
                    expect(page.get_by_role("heading", name="Operator sign-in")).to_be_visible()
                    expect(page.get_by_role("heading", name="Every venture deserves", exact=False)).to_have_count(0)
                    assert not calls
                    context.close()
                    continue

                expect(page.get_by_role("heading", name="Every venture deserves", exact=False)).to_be_visible()
                build = page.get_by_role("button", name="Build landing page")
                expect(build).to_be_disabled()
                page.get_by_label("Venture ID").fill("ven_synthetic")
                page.get_by_role("button", name="Estimate build").click()
                expect(page.get_by_text("Up to 5.25 credits", exact=False)).to_be_visible()
                expect(build).to_be_enabled()
                build.click()
                expect(page.get_by_role("heading", name="Ready for review.")).to_be_visible(timeout=30000)
                frame = page.locator("iframe")
                expect(frame).to_have_count(1)
                assert frame.get_attribute("sandbox") == ""
                expect(page.frame_locator("iframe").get_by_role("heading", name="Synthetic preview fixture")).to_be_visible()
                assert calls == [("/api/ventures/ven_synthetic/pagebuilder/estimate", {"variants": 1}), ("/api/ventures/ven_synthetic/pagebuilder/start", {"variants": 1, "quote_fingerprint": quote_fingerprint})]
                # An idempotent start returns the same job ID; its saved preview
                # must load again rather than leaving an empty ready canvas.
                expect(build).to_be_enabled()
                with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/preview")):
                    build.click()
                expect(page.get_by_role("heading", name="Ready for review.")).to_be_visible(timeout=30000)
                expect(page.frame_locator("iframe").get_by_role("heading", name="Synthetic preview fixture")).to_be_visible(timeout=30000)
                assert len(calls) == 3 and calls[-1][0].endswith("/start")
                page.screenshot(path=str(output / "lab-desktop.png"), full_page=True)
                page.get_by_role("button", name="Mobile", exact=True).click()
                assert frame.bounding_box()["width"] <= 390
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(output / "lab-mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                # Changing the venture retires every previous receipt and preview.
                page.get_by_label("Venture ID").fill("ven_another")
                expect(page.locator("iframe")).to_have_count(0)
                expect(page.get_by_role("button", name="Build landing page")).to_be_disabled()
                page.get_by_label("Venture ID").fill("ven_failed")
                page.get_by_role("button", name="Estimate build").click()
                expect(build).to_be_enabled()
                build.click()
                page.get_by_text("Inspect stopped build receipt").click()
                expect(page.get_by_text("$0.0190 confirmed provider spend.", exact=False)).to_be_visible()
                expect(page.get_by_text("remains unresolved", exact=False)).to_be_visible()
                expect(page.locator("iframe")).to_have_count(0)
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.get_by_label("Venture ID").fill("ven_variants")
                page.get_by_role("combobox").select_option("3")
                page.get_by_role("button", name="Estimate build").click()
                expect(build).to_be_enabled()
                build.click()
                expect(page.locator("iframe")).to_have_count(3, timeout=30000)
                expect(page.get_by_role("button", name="Download source + art · Direction 3", exact=True)).to_be_visible()
                page.get_by_role("button", name="Inspect coding spec · Direction 3", exact=True).click()
                expect(page.get_by_text("Synthetic selected-direction coding spec", exact=True)).to_be_visible()
                assert exports and urlsplit(exports[-1]).query.endswith("variant=2")
                context.close()
        finally:
            browser.close()
    return {"admin_gate": "passed", "estimate_start_preview": "passed", "sandbox": "passed", "responsive": "passed", "scope_reset": "passed", "stopped_receipt": "passed", "variant_selection": "passed", "mode": "synthetic_http_and_mac_bridge_ui_check"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3100")
    parser.add_argument("--output", type=Path, default=Path("/tmp/supymem-pagebuilder-lab-check"))
    args = parser.parse_args()
    print(json.dumps(check(args.url, args.output), sort_keys=True))

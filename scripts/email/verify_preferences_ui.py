"""Exercise running console and signed email settings against an isolated local API.

Run with the console dev server already listening; no real provider, account or mail is used.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "packages")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--console-url", default="http://127.0.0.1:3134")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/supymem/email/verification")
    args = parser.parse_args()
    if urlsplit(args.console_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Only an owned loopback console is allowed")
    from playwright.sync_api import expect, sync_playwright
    from tests.company_os_harness import load_company_os_app
    from tests.integration.test_postgres_email_delivery import running_api

    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="email-ui-proof-") as temp:
        os.environ.update(COMPANY_OS_DB_URL=f"sqlite:///{temp}/proof.db", COMPANY_OS_DATA_DIR=temp,
                          EMAIL_PROVIDER="log", LLM_OFFLINE="1", CHANNELS_DRY_RUN="true",
                          COS_INTERNAL_ASSERTION_SECRET="isolated-email-browser-secret-32-chars",
                          COMPANY_OS_REQUIRE_SIGNED_CALLER="0", EMAIL_PHYSICAL_ADDRESS="Fixture address")
        main_app = load_company_os_app()
        from app import state
        from app.db.models import Person
        from app.email import service
        actor = "usr_email_browser_fixture"
        with state.db()() as session:
            session.add(Person(id=actor, display_name="Maya", email=None, created_at=service.utcnow()))
            session.commit()
        with running_api(main_app.create_app()) as api, sync_playwright() as playwright:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            browser = playwright.chromium.launch(**({"executable_path": str(chrome)} if chrome.is_file() else {}))
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            gateway = "http://127.0.0.1:3200"
            writes = []
            fail_save = [False]

            def route_request(route):
                request = route.request
                parsed = urlsplit(request.url)
                if request.url.startswith(gateway):
                    path = parsed.path
                    if request.method == "OPTIONS":
                        return route.fulfill(status=204, headers={"Access-Control-Allow-Origin": args.console_url,
                            "Access-Control-Allow-Credentials": "true", "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "Content-Type,X-CSRF-Token"),
                            "Access-Control-Allow-Methods": "GET,PUT,POST,OPTIONS"})
                    if path == "/auth/session":
                        body = {"authenticated": True, "subject": actor, "csrf": "fixture-csrf"}
                    elif path == "/api/email/preferences":
                        if request.method == "PUT" and fail_save[0]:
                            return route.fulfill(status=503, json={"detail": "fixture_save_failed"},
                                headers={"Access-Control-Allow-Origin": args.console_url, "Access-Control-Allow-Credentials": "true"})
                        response = api.request(request.method, "/email/preferences", headers={"X-Subject": actor},
                                               json=request.post_data_json if request.method == "PUT" else None)
                        if request.method == "PUT":
                            writes.append(response.status_code)
                        return route.fulfill(status=response.status_code, json=response.json(),
                            headers={"Access-Control-Allow-Origin": args.console_url, "Access-Control-Allow-Credentials": "true"})
                    elif path == "/api/ventures":
                        body = {"ventures": []}
                    elif path == "/api/me/greeting":
                        body = {"first_name": "Maya"}
                    elif path == "/api/beta/codes":
                        body = {"codes": []}
                    else:
                        body = {}
                    return route.fulfill(json=body, headers={"Access-Control-Allow-Origin": args.console_url,
                                                           "Access-Control-Allow-Credentials": "true"})
                if parsed.hostname not in {"127.0.0.1", "localhost"}:
                    return route.abort()
                return route.continue_()

            context.route("**/*", route_request)
            page = context.new_page()
            page.set_default_timeout(60_000)
            page.set_default_navigation_timeout(180_000)
            errors = []
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            page.goto(args.console_url + "/account", wait_until="domcontentloaded")
            section = page.get_by_role("region", name="Email preferences")
            expect(section).to_be_visible()
            digest = section.get_by_role("checkbox", name="Daily and weekly digests")
            expect(digest).not_to_be_checked()
            digest.check()
            expect(digest).to_be_enabled()
            assert service.get_prefs(actor)["digests"] is True
            page.reload()
            digest = page.get_by_role("region", name="Email preferences").get_by_role("checkbox", name="Daily and weekly digests")
            expect(digest).to_be_checked()
            fail_save[0] = True
            digest.uncheck()
            expect(page.get_by_text("Could not save that change. Try again.")).to_be_visible()
            expect(digest).to_be_checked()
            assert service.get_prefs(actor)["digests"] is True
            fail_save[0] = False
            page.screenshot(path=str(args.out / "console-preferences.png"), full_page=True)
            token = service.make_token(actor, "all")
            settings_path = "/public/email/unsubscribe?t=" + token + "&view=preferences"
            page.goto(str(api.base_url).rstrip("/") + settings_path)
            expect(page.get_by_role("heading", name="Your email settings")).to_be_visible()
            expect(page.get_by_role("checkbox", name="Daily and weekly digests")).to_be_checked()
            page.get_by_role("checkbox", name="Daily and weekly digests").uncheck()
            page.get_by_role("button", name="Save preferences").click()
            expect(page.get_by_role("status")).to_have_text("Your preferences are saved.")
            assert service.get_prefs(actor)["digests"] is False
            for theme in ("light", "dark"):
                page.emulate_media(color_scheme=theme)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(args.out / f"public-preferences-mobile-{theme}.png"), full_page=True)
            unsub = str(api.base_url).rstrip("/") + "/public/email/unsubscribe?t=" + service.make_token(actor, "product_updates")
            page.goto(unsub)
            assert service.get_prefs(actor)["product_updates"] is True
            page.get_by_role("button", name="Unsubscribe", exact=True).click()
            expect(page.get_by_role("heading")).to_contain_text("You're unsubscribed")
            assert service.get_prefs(actor)["product_updates"] is False
            assert writes == [200] and errors == []
            browser.close()
        report = {"verified": True, "provider": "log", "external_requests": "blocked",
                  "checks": ["console digests default off", "save persists on reload", "failed save rolls back",
                             "signed public form saves", "mobile light/dark no overflow", "GET scanner safe", "unsubscribe POST works"],
                  "console_save_statuses": writes, "page_errors": len(errors)}
        (args.out / "preferences-ui.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()

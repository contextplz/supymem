#!/usr/bin/env python3
"""Eight synthetic ventures through the real pagebuilder, then independent browser QA.

Live runners must reserve/meter calls and return their receipt. Offline callers are
explicit contract exercises; the report never labels them live generation evidence.
No HTML or visual templates are supplied by this harness.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import hashlib
import html
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "services/company_os"), str(ROOT / "packages"), str(ROOT)]
PHASH_MIN_DISTANCE = 10
MAX_SPEND_USD = 10.0
REQUEST_POLICY = "scoped_grounding_v19"
GROUNDING_POLICY = "semantic_capability_v4"
IMAGE_COMPILER_VERSION = "native_scene_v2"
IMAGE_PROVENANCE = "model_scene_native_svg"
REASONING_POLICY = {"planning": "low", "reviewer": "low", "generation": "none"}
REASONING_PROFILES = {
    ("azure_foundry", "DeepSeek-V4-Pro"): REASONING_POLICY,
    ("openai_compatible", "gpt-6-luna"): {phase: None for phase in REASONING_POLICY},
    ("openai", "gpt-6.1-sol"): {"planning": "low", "reviewer": "low", "generation": "low"},
}
CALL_TIMEOUT_SECONDS = 300
FIXTURES = [
    {"slug": "nightshift", "name": "Nightshift", "fixture_version": 2, "category": "B2B ops tool", "idea": "A proposed digital/web software shift-handoff board for independent warehouse teams. Supervisors would leave unresolved tasks, equipment notes and shift priorities for the next crew. No inventory integrations, predictive AI or live tracking are promised.", "audience": "Warehouse supervisors and incoming shift leads"},
    {"slug": "smallhours", "name": "Small Hours", "category": "Consumer health", "idea": "A concept for a private sleep journal for adults whose work hours change every week. It would pair a simple personal journal with reflection prompts about bedtime routines. It is not a medical service and will not diagnose or treat sleep disorders.", "audience": "Adults working rotating shifts"},
    {"slug": "patchwork", "name": "Patchwork", "category": "Developer tool", "idea": "A proposed local-first changelog notebook for maintainers of small open-source projects. Maintainers would collect release notes and link decisions to commits, then export plain Markdown. No automated code repair, integrations or shipped CLI is claimed.", "audience": "Open-source maintainers preparing releases"},
    {"slug": "nearbyhands", "name": "Nearby Hands", "category": "Local services marketplace", "idea": "A proposed neighborhood directory for small home-repair jobs. Residents would describe a repair and explore availability from nearby independent repair professionals. There are no verified tradespeople, guarantees, prices or instant bookings yet.", "audience": "Apartment residents needing small repairs"},
    {"slug": "afterimage", "name": "Afterimage", "fixture_version": 2, "category": "Creator tool", "idea": "A planned digital/web software visual reference notebook for independent film essay creators. Creators would arrange scene notes, color references and narration ideas into a board for their next essay. The product will not host copyrighted film stills or generate celebrity images.", "audience": "Independent video essay creators"},
    {"slug": "margin", "name": "Margin", "category": "Fintech for freelancers", "idea": "A concept for a cash-flow planning notebook for freelance designers. Freelancers would manually organize upcoming invoices and planned expenses into a monthly view. No bank connectivity, tax filing, financial advice or automatic payments are planned for the first concept.", "audience": "Freelance designers with uneven monthly income"},
    {"slug": "wondertrail", "name": "Wondertrail", "category": "Edtech for parents", "idea": "A proposed collection of parent-led weekend nature activities for children. Parents would choose short outdoor observation activities and keep a family discovery notebook. No child accounts, learning-outcome claims or expert endorsements are available.", "audience": "Parents of primary-school children"},
    {"slug": "releaf", "name": "Releaf", "category": "Climate hardware", "idea": "A concept-stage rainwater monitor for small urban gardens. The proposed hardware would show the level in a collection barrel and support a simple manual watering journal. No working hardware, environmental savings numbers or shipping date is confirmed.", "audience": "Urban gardeners with rainwater barrels"},
]


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return value


def phash(path: Path) -> int:
    """64-bit DCT pHash; comparison uses Hamming distance, never file hashes."""
    from PIL import Image
    with Image.open(path) as image:
        resized = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
        pixels = [resized.getpixel((x, y)) for y in range(32) for x in range(32)]
    cosines = [[math.cos((2 * x + 1) * u * math.pi / 64) for x in range(32)] for u in range(8)]
    rows = [[sum(pixels[y * 32 + x] * cosines[u][x] for x in range(32)) for u in range(8)] for y in range(32)]
    frequencies = [sum(rows[y][u] * cosines[v][y] for y in range(32)) for v in range(8) for u in range(8)]
    threshold = statistics.median(frequencies[1:])
    return sum((frequency > threshold) << index for index, frequency in enumerate(frequencies))


def direction_values(direction: dict) -> dict:
    palette = direction.get("palette", {})
    fonts = direction.get("font_pair") or [direction.get("display_font", ""), direction.get("body_font", "")]
    if isinstance(fonts, str):
        fonts = [fonts]
    return {"palette": palette, "fonts": [str(font).strip().casefold() for font in fonts], "layout": str(direction.get("layout_archetype", "")).strip().casefold(),
            "palette_family": str(direction.get("palette_family", "")).strip().casefold(), "concept": direction.get("concept", direction.get("name", ""))}


def palette_distance(left: dict, right: dict) -> float | None:
    roles = sorted(set(left) & set(right))
    distances = []
    for role in roles:
        if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", str(p[role])) for p in (left, right)):
            continue
        rgb = [[int(p[role][i:i + 2], 16) for i in (1, 3, 5)] for p in (left, right)]
        distances.append(math.dist(*rgb) / math.sqrt(3))
    return round(statistics.mean(distances), 2) if distances else None


def diversity(rows: list[dict]) -> dict:
    pairs = []
    measured = [row for row in rows if row.get("hero_phash")]
    for left, right in itertools.combinations(rows, 2):
        a, b = left.get("direction", {}), right.get("direction", {})
        distance = ((int(left["hero_phash"], 16) ^ int(right["hero_phash"], 16)).bit_count()
                    if left.get("hero_phash") and right.get("hero_phash") else None)
        color_distance = palette_distance(a.get("palette", {}), b.get("palette", {}))
        duplicate = all(a.get(key) == b.get(key) for key in ("fonts", "layout")) and (a.get("palette_family") == b.get("palette_family") or color_distance is not None and color_distance < 18)
        pairs.append({"left": left["slug"], "right": right["slug"], "palette_distance": color_distance,
                      "hero_phash_distance": distance, "duplicate_direction": duplicate,
                      "passed": bool(not duplicate and distance is not None and distance > PHASH_MIN_DISTANCE)})
    return {"hero_phash_minimum_exclusive": PHASH_MIN_DISTANCE, "font_pair_count": len({tuple(r.get("direction", {}).get("fonts", [])) for r in measured}),
            "layout_count": len({r.get("direction", {}).get("layout", "") for r in measured}), "pairs": pairs,
            "passed": len(rows) == 8 and len(pairs) == 28 and all(p["passed"] for p in pairs)}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def serve(directory: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def browser_checks(bundle: Path, origin: str, expected_waitlist: str, *, require_analytics=False, consent_text="") -> dict:
    from playwright.sync_api import sync_playwright
    from core.public_copy import public_copy_observation
    from app.firstlook.lint import capability_claims
    from app.pagebuilder.qa import waitlist_copy_failures
    from app.pagebuilder.rendering import prime_images
    from services.qa_runner.app.static_qa_runner import ANALYTICS_CAPTURE
    evidence = {"viewports": {}, "waitlist_destination": expected_waitlist, "passed": False}
    safe_env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH"}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True, chromium_sandbox=True, env=safe_env)
        evidence["browser_version"] = browser.version
        try:
            for label, width, height in (("desktop", 1440, 900), ("mobile", 390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height}, service_workers="block", accept_downloads=False)
                context.add_init_script(ANALYTICS_CAPTURE)
                blocked, errors = [], []

                def route(request_route):
                    url = urlsplit(request_route.request.url)
                    if request_route.request.method == "GET" and (f"{url.scheme}://{url.netloc}" == origin or url.hostname in {"fonts.googleapis.com", "fonts.gstatic.com"}):
                        request_route.continue_()
                    else:
                        blocked.append("external_request")
                        request_route.abort()

                context.route("**/*", route)
                context.route_web_socket("**/*", lambda _socket: blocked.append("websocket"))
                page = context.new_page()
                page.on("pageerror", lambda _error: errors.append("page_exception"))
                page.on("console", lambda msg: errors.append("console_error") if msg.type == "error" else None)
                response = page.goto(origin + "/index.html", wait_until="networkidle", timeout=15000, referer="http://qa-referrer.example/source")
                page.emulate_media(reduced_motion="reduce")
                image_failures = prime_images(page)
                page.screenshot(path=str(bundle / f"{label}.png"), full_page=True, animations="disabled")
                if label == "desktop":
                    page.screenshot(path=str(bundle / "hero.png"), full_page=False, animations="disabled")
                text = page.locator("body").inner_text()
                copy_failures, _ = public_copy_observation(text)
                claims = capability_claims(text)
                signup_copy = "\n".join(page.locator("#waitlist,footer").all_inner_texts())
                signup_failures = waitlist_copy_failures(signup_copy, consent_text)
                observed = page.evaluate("""() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth + 2,
                    title: !!document.title.trim(), h1: document.querySelectorAll('h1').length,
                    landmarks: document.querySelectorAll('main').length===1, header:!!document.querySelector('header'),
                    images: Array.from(document.images).map(i => ({src:i.getAttribute('src'),alt:i.getAttribute('alt'),loaded:i.complete&&i.naturalWidth>0,width:i.getBoundingClientRect().width,height:i.getBoundingClientRect().height})),
                    forms: Array.from(document.forms).map(f => ({action:f.action,method:f.method,email:!!f.querySelector('input[type=email][name=email]'),labelled:Array.from(f.querySelectorAll('input:not([type=hidden])')).every(i => i.labels?.length || i.getAttribute('aria-label'))})),
                    links: Array.from(document.querySelectorAll('a[href]')).map(a=>({href:a.href,rel:a.rel,target:a.target,name:a.innerText.trim()})),
                    fonts:{heading:getComputedStyle(document.querySelector('h1')||document.body).fontFamily,body:getComputedStyle(document.body).fontFamily},
                    brokenAnchors:Array.from(document.querySelectorAll('a[href^="#"]')).filter(a=>a.hash.length>1&&!document.getElementById(a.hash.slice(1))).length,
                    analyticsScripts:document.querySelectorAll('script[data-supymem-analytics="1"]').length
                })""")
                controls = page.locator("a[href],form")
                waitlist = [link for link in observed["links"] if link["href"] == expected_waitlist]
                forms = [form for form in observed["forms"] if form["action"] == expected_waitlist]
                safe_links = all(link["target"] == "_blank" and {"noopener", "noreferrer"}.issubset(set(link["rel"].split())) for link in waitlist)
                activation = False
                if waitlist:
                    candidate = next((controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible() and controls.nth(i).get_attribute("href") == expected_waitlist), None)
                    if candidate:
                        candidate.evaluate("el => el.addEventListener('click', event => {event.preventDefault(); window.__evalWaitlistActivated=true}, {capture:true})")
                        candidate.click()
                        activation = bool(page.evaluate("window.__evalWaitlistActivated"))
                elif forms:
                    candidate = page.locator("form").filter(has=page.locator("input[type=email][name=email]")).first
                    candidate.evaluate("el => el.addEventListener('submit', event => {event.preventDefault(); window.__evalWaitlistActivated=true}, {capture:true})")
                    candidate.locator("input[type=email][name=email]").fill("synthetic-eval@example.invalid")
                    candidate.locator("button[type=submit],input[type=submit]").first.click()
                    activation = bool(page.evaluate("window.__evalWaitlistActivated"))
                page.wait_for_timeout(100)
                events = page.evaluate("window.__supymemQaEvents || []")
                views = [event for event in events if event.get("event") == "landing_view"]
                conversions = [event for event in events if event.get("event") == "cta_click"]
                analytics_ok = (not require_analytics and observed["analyticsScripts"] == 0) or (observed["analyticsScripts"] == 1 and len(views) == 1 and len(conversions) == 1 and views[0].get("props", {}).get("referrer_host") == "qa-referrer.example")
                ok = bool(response and response.status == 200 and observed["title"] and observed["h1"] == 1 and observed["landmarks"]
                          and not observed["overflow"] and not observed["brokenAnchors"] and len(observed["images"]) >= 2
                          and all(image["alt"] and image["loaded"] for image in observed["images"])
                          and (waitlist and safe_links or forms and all(f["email"] and f["labelled"] for f in forms))
                          and activation and analytics_ok and not image_failures and not copy_failures and not claims and not signup_failures and not blocked and not errors)
                evidence["viewports"][label] = {**observed, "copy_failures": copy_failures, "capability_claim_count": len(claims), "blocked_requests": len(blocked),
                                                "waitlist_copy_failures": signup_failures, "browser_errors": len(errors), "waitlist_activation_intercepted": activation, "passed": ok}
                evidence["viewports"][label]["image_failures"] = image_failures
                evidence["viewports"][label]["analytics"] = {"passed": analytics_ok, "pageviews": len(views), "conversions": len(conversions), "requests_intercepted": True}
                context.close()
        finally:
            browser.close()
    evidence["passed"] = all(v["passed"] for v in evidence["viewports"].values())
    return evidence


def lighthouse(bundle: Path, origin: str, command: str | None) -> dict:
    executable = command or shutil.which("lighthouse")
    argv = [executable] if executable else ["npm", "exec", "--yes", "--package=lighthouse@13.5.0", "--", "lighthouse"]
    argv += [origin + "/index.html", "--quiet", "--only-categories=performance,accessibility", "--output=json", f"--output-path={bundle / 'lighthouse.json'}", "--chrome-flags=--headless", "--blocked-url-patterns=https://*"]
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.exists():
        env["CHROME_PATH"] = str(chrome)
    try:
        result = subprocess.run(argv, env=env, capture_output=True, timeout=150)
        if result.returncode:
            return {"passed": False, "error": "lighthouse_process_failed"}
        report = json.loads((bundle / "lighthouse.json").read_text())
        scores = {key: round(report["categories"][key]["score"] * 100) for key in ("performance", "accessibility")}
        failed_audits = [key for key, audit in report.get("audits", {}).items() if audit.get("score") == 0]
        return {**scores, "version": report.get("lighthouseVersion"), "form_factor": report.get("configSettings", {}).get("formFactor"), "failed_audits": failed_audits,
                "passed": scores["performance"] >= 90 and scores["accessibility"] >= 90 and report.get("configSettings", {}).get("formFactor") == "mobile"}
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, json.JSONDecodeError):
        return {"passed": False, "error": "lighthouse_unavailable_or_incomplete"}


def write_bundle(output: Path, built) -> dict:
    from core.artifact_policy import safe_name
    built = plain(built)
    source = built.get("files", {})
    if not source or "index.html" not in source:
        raise ValueError("runner_missing_generated_files")
    for name, value in source.items():
        safe_name(name)
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode() if isinstance(value, str) else value)
    metadata = {key: plain(value) for key, value in built.items() if key not in {"files", "screenshots"}}
    (output / "bundle.json").write_text(json.dumps(metadata, indent=2, default=str))
    return metadata


def receipt_spend(directory: Path | None, *, slug: str | None = None, field="provider_cost_usd") -> float:
    if not directory or not directory.exists():
        return 0.0
    receipts = directory.glob((slug + "-" if slug else "") + "*.json")
    # Failed builds still spend provider tokens: account from durable receipts,
    # not only successfully returned pages.
    return sum(float(json.loads(path.read_text()).get(field, 0)) for path in receipts)


def grounding_checks(metadata: dict, fixture: dict | None = None) -> dict:
    """Bind the generated plan review to its actual settled metered operation.

    This generation evidence does not replace independent review or release QA.
    """
    from app.pagebuilder.models import CapabilityReview, PageInput, PagePlan
    from app.pagebuilder.pipeline import public_plan_copy, validate_grounding_review
    from app.pagebuilder.images import validate_compiled_assets

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    failures = []
    try:
        receipt = metadata["cost_receipt"]
        provider, model = receipt["provider"], receipt["model"]
        expected_reasoning = REASONING_PROFILES[(provider, model)]
        if receipt.get("request_policy") != REQUEST_POLICY:
            failures.append("grounding_request_policy_obsolete")
        if receipt.get("reasoning_policy") != expected_reasoning:
            failures.append("grounding_reasoning_policy_mismatch")
        if receipt.get("call_timeout_seconds") != CALL_TIMEOUT_SECONDS:
            failures.append("grounding_call_deadline_mismatch")
        inputs = PageInput.model_validate(receipt["grounding_input"])
        if receipt.get("grounding_input_sha256") != digest(inputs.model_dump(mode="json")):
            failures.append("grounding_input_digest_mismatch")
        if fixture and (inputs.name != fixture["name"] or inputs.idea != fixture["idea"]
                        or inputs.clarified_answers != {"audience": fixture["audience"], "category": fixture["category"]}):
            failures.append("grounding_synthetic_fixture_mismatch")
        backend = receipt.get("waitlist_backend", {})
        if (inputs.waitlist_url != receipt.get("waitlist_url")
                or inputs.waitlist_consent_text != backend.get("consent_text")
                or inputs.waitlist_consent_version != backend.get("consent_version")):
            failures.append("grounding_waitlist_input_mismatch")
        plan = PagePlan.model_validate(metadata["plan"])
        validate_compiled_assets(plan)
        manifest = metadata["manifest"]
        if (not isinstance(manifest, dict) or manifest.get("image_compiler_version") != IMAGE_COMPILER_VERSION
                or manifest.get("image_provenance") != IMAGE_PROVENANCE):
            failures.append("grounding_image_derivation_provenance_invalid")
        qa = metadata["qa"]
        rendered = qa["render"]
        if (qa.get("passed") is not True or rendered.get("passed") is not True
                or rendered.get("browser_rendered") is not True or rendered.get("failures") != []):
            failures.append("grounding_renderer_evidence_invalid")
        for viewport in ("desktop", "mobile"):
            observed = rendered["observations"][viewport]
            checks = observed["checks"]
            hero = observed["initialHeroCta"]
            if (any(checks.get(check) is not True for check in ("contrast", "image_clipping", "hero_cta_initial"))
                    or observed.get("contrastSkipped") != 0 or observed.get("contrastUnresolved") != []
                    or observed.get("imageClippingFailures") != []
                    or hero.get("visible") is not True or hero.get("withinViewport") is not True
                    or hero.get("hitCoverage") is not True):
                failures.append("grounding_renderer_viewport_evidence_invalid:" + viewport)
        plan_hash = digest(plan.model_dump(mode="json"))
        grounding = metadata["qa"]["capability_grounding"]
        review = grounding["reviews"][-1]
        stage = review["stage"]
        response = CapabilityReview.model_validate(review["review"])
        validate_grounding_review(response, plan, inputs)
        reviewed_fields = [field.field_id for field in response.field_reviews]
        if len(reviewed_fields) != len(set(reviewed_fields)) or set(reviewed_fields) != set(public_plan_copy(plan)):
            failures.append("grounding_public_field_coverage_invalid")
        if (grounding.get("policy") != GROUNDING_POLICY or grounding.get("passed") is not True
                or grounding.get("scope") != "generation_only" or grounding.get("independent_qa_attestation") is not False):
            failures.append("grounding_scope_or_policy_invalid")
        if grounding.get("plan_sha256") != plan_hash or review.get("plan_sha256") != plan_hash:
            failures.append("grounding_plan_digest_mismatch")
        review_hash = digest(response.model_dump(mode="json"))
        if review.get("review_sha256") != review_hash:
            failures.append("grounding_review_digest_mismatch")
        if stage not in {f"capability_review_{index}" for index in range(1, 7)} or response.approved is not True or response.unsupported_claims:
            failures.append("grounding_final_review_not_approved")
        operations = receipt.get("grounding_operations", [])
        matches = [operation for operation in operations if operation.get("operation_key") == "variant_0:" + stage]
        if (len(matches) != 1 or matches[0].get("stage") != stage or matches[0].get("state") != "completed"
                or matches[0].get("settled") is not True or matches[0].get("review_sha256") != review_hash
                or matches[0].get("provider") != provider or matches[0].get("model") != model):
            failures.append("grounding_metered_operation_missing_or_mismatched")
    except (KeyError, IndexError, TypeError, ValueError):
        return {"passed": False, "failures": ["grounding_evidence_missing_or_invalid"], "independent_qa_attestation": False}
    return {"passed": not failures, "failures": failures, "plan_sha256": plan_hash,
            "stage": stage, "reviewed_public_fields": len(reviewed_fields), "scope": "generation_only", "independent_qa_attestation": False}


def independent_review(output: Path, row: dict) -> dict:
    """A separate reviewer must inspect and approve these exact final bytes."""
    try:
        directory = (output / row.get("artifact_path", row["slug"])).resolve()
        if not directory.is_relative_to(output.resolve()):
            raise ValueError("review_path_invalid")
        review = json.loads((directory / "independent-review.json").read_text())
        source_hash = hashlib.sha256((directory / "index.html").read_bytes()).hexdigest()
        passed = (review.get("approved") is True and review.get("source_sha256") == source_hash
                  and review.get("reviewer") == "/root")
        return {"passed": passed, "source_sha256": source_hash, "reviewer": review.get("reviewer"),
                "reviewed_at": review.get("reviewed_at"), "scope": "visual_and_factual_review", "release_qa_attestation": False}
    except (OSError, KeyError, TypeError, ValueError):
        return {"passed": False, "error": "independent_review_missing_or_invalid", "release_qa_attestation": False}


def write_report(output: Path, rows: list[dict], mode: str, spend: float, budget_usd: float = MAX_SPEND_USD):
    distinct = diversity(rows)
    unresolved = receipt_spend(output / "receipts", field="unresolved_ceiling_usd")
    reviews = [{"slug": row["slug"], **independent_review(output, row)} for row in rows]
    reviewed = len(reviews) == 8 and all(review["passed"] for review in reviews)
    passed = reviewed and len(rows) == 8 and all(row.get("passed") for row in rows) and distinct["passed"] and mode == "live" and spend + unresolved <= budget_usd
    attempts_path = output / "attempts.json"
    attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
    profiles = [{"provider": provider, "model": model, "reasoning_policy": policy}
                for (provider, model), policy in REASONING_PROFILES.items()]
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": mode, "passed": passed, "total_spend_usd": round(spend, 6),
              "unresolved_ceiling_usd": round(unresolved, 6), "spend_cap_usd": budget_usd, "nightly_cap_usd": MAX_SPEND_USD,
              "billing_reconciled": unresolved == 0, "required_request_policy": REQUEST_POLICY,
              "required_grounding_policy": GROUNDING_POLICY, "required_reasoning_profiles": profiles,
              "required_image_compiler_version": IMAGE_COMPILER_VERSION, "required_image_provenance": IMAGE_PROVENANCE,
              "required_call_timeout_seconds": CALL_TIMEOUT_SECONDS,
              "required_fixture_versions": {fixture["slug"]: fixture.get("fixture_version", 1) for fixture in FIXTURES},
              "diversity": distinct, "independent_reviews": reviews, "ventures": rows, "attempts": attempts}
    (output / "report.json").write_text(json.dumps(report, indent=2))
    cards = []
    lines = ["# Pagebuilder evaluation", "", f"Mode: **{mode}**. Live quality acceptance: **{'PASS' if passed else 'NOT PASSED'}**. Known provider spend: **${spend:.6f} / ${budget_usd:g} run cap ($10 nightly cap)**. Unresolved reserved exposure: **${unresolved:.6f}**. Billing reconciled: **{'yes' if unresolved == 0 else 'no'}**.", "",
             "Synthetic ideas only. All screenshots come from the actual returned pipeline bytes; this harness contains no page templates. Offline mode is contract evidence and cannot pass live acceptance.", "",
             "Nightshift and Afterimage fixture v2 explicitly clarify digital/web software context. Earlier attempts retain their original confirmed input and receipts; these clarifications do not authorize prior rejected claims.", "",
             f"Required generation evidence: `{REQUEST_POLICY}` and `{GROUNDING_POLICY}`, with complete public-field coverage, exact source quotes and typed review hashes bound to settled metered operations. The quoted provider/model must match the actual final settled review operation. Only the following captured route profiles are eligible; the captured call deadline must be {CALL_TIMEOUT_SECONDS} seconds.", "",
             "| Provider | Model | Planning | Review | HTML |", "| --- | --- | --- | --- | --- |",
             *[f"| {profile['provider']} | {profile['model']} | " + " | ".join(
                 str(value) if value is not None else "null (default)" for value in profile["reasoning_policy"].values()) + " |" for profile in profiles], "",
             "OpenAI null values record the captured default behavior; no explicit reasoning-effort setting is claimed. Missing or mismatched route evidence fails closed.", "",
             f"Current image derivation requires `{IMAGE_COMPILER_VERSION}` and `{IMAGE_PROVENANCE}` in the manifest, with the shared compiler validating every normalized scene, effective palette, SVG and alt. This derivation evidence makes no vision, aesthetic or release-QA claim.", "",
             "Both saved browser viewport observations must record complete text-contrast coverage, no unresolved background or image clipping, and a fully visible, unobstructed hero CTA measured before scrolling. Missing or old renderer evidence fails closed; numerical Lighthouse scores cannot substitute for these observations.", "",
             "Final acceptance also requires separate visual and factual approval bound to the exact final HTML hash. These review records do not attest native release QA.", "",
             "Provider USD records the displayed bundle's original receipt cost on resume, or the newly settled attempt cost for a fresh run. All fixture attempts USD includes historical rejected jobs for that slug; total spend includes every durable receipt.", "",
             "| Venture | Mobile performance | Accessibility | Browser QA | Independent review | Numerical gates | Generation seconds | Provider USD | All fixture attempts USD |", "| --- | ---: | ---: | --- | --- | --- | ---: | ---: | ---: |"]
    for row, review in zip(rows, reviews):
        slug, name = row["slug"], row["name"]
        artifact_path = html.escape(row.get("artifact_path", slug), quote=True)
        scores = row.get("lighthouse", {})
        lines.append(f"| {name} | {scores.get('performance', '—')} | {scores.get('accessibility', '—')} | {'pass' if row.get('browser', {}).get('passed') else 'fail'} | {'approved' if review['passed'] else 'pending/rejected'} | {'pass' if row.get('passed') else row.get('error', 'fail')} | {row.get('generation_seconds', 0):.2f} | {row.get('cost_usd', 0):.6f} | {row.get('cumulative_fixture_spend_usd', row.get('cost_usd', 0)):.6f} |")
        shots = f'<div class="shots"><a href="{artifact_path}/index.html"><img src="{artifact_path}/desktop.png" alt="{html.escape(name)} desktop screenshot"></a><img class="mobile" src="{artifact_path}/mobile.png" alt="{html.escape(name)} mobile screenshot"></div>' if row.get("hero_phash") else "<p>No generated browser evidence.</p>"
        error = f"<p>{html.escape(row.get('error', ''))}</p>" if row.get("error") else ""
        cards.append(f'<article><h2>{html.escape(name)} <small>{html.escape(row["category"])}</small></h2><p>Performance {scores.get("performance", "unverified")} · Accessibility {scores.get("accessibility", "unverified")} · ${row.get("cost_usd", 0):.6f} · {row.get("generation_seconds", 0):.2f}s generation</p>{error}{shots}</article>')
    lines += ["", f"Diversity: {distinct['font_pair_count']} font pairs, {distinct['layout_count']} layout archetypes; {sum(p['passed'] for p in distinct['pairs'])}/28 pair checks passed.", "",
              f"pHash uses a 32×32 grayscale DCT and 64 low-frequency bits. Every desktop first-viewport pair must have Hamming distance **>{PHASH_MIN_DISTANCE}/64**. This project threshold detects near duplicates; it is not a universal measure of design quality. Palette distance is mean role-wise Euclidean RGB distance normalized by √3. A font-pair/layout/palette-family triple may not repeat.", "",
              "Lighthouse runs mobile simulated throttling in an isolated Chrome profile. Local laboratory scores do not certify production hosting or all accessibility requirements. Waitlist destinations and real control activation are verified with intercepted navigation; no real signup is submitted. Original images must load with descriptive alt text, and both viewports must have no horizontal overflow, broken anchors or browser errors.", "",
              "Sources: [Lighthouse performance scoring](https://developer.chrome.com/docs/lighthouse/performance/performance-scoring), [Lighthouse accessibility scoring](https://developer.chrome.com/docs/lighthouse/accessibility/scoring), [pHash design](https://www.phash.org/docs/design.html).", "",
              "Failures are retained in [gallery/report.json](gallery/report.json), including generation failures, audit errors and every pairwise comparison. Desktop/mobile screenshots and raw Lighthouse JSON sit beside each generated page.", ""]
    if attempts:
        lines += [f"Observed attempts: **{len(attempts)}**. Billed tokens remain included in total spend; numerical passes still require independent factual and visual approval.", ""]
        for attempt in attempts:
            error = attempt.get("error") or attempt.get("factual_review_error")
            state = f"`{error}`" if error else ("numerical gates passed; independent review pending" if attempt.get("passed") else "rejected")
            lines.append(f"- {attempt['slug']}: {state}, ${attempt.get('cost_usd', 0):.6f}.")
        lines.append("")
    rejected = sorted(output.glob("rejected/*/README.md"))
    if rejected:
        lines += ["Rejected paid candidates are retained for diagnosis only and do not count toward live acceptance:", ""]
        lines += [f"- [{path.parent.name}](gallery/{path.relative_to(output).as_posix()})" for path in rejected]
        lines.append("")
    (output.parent / "EVAL_REPORT.md").write_text("\n".join(lines))
    (output / "index.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pagebuilder evaluation gallery</title><style>body{font:16px system-ui,sans-serif;background:#f1f4f9;color:#162037;margin:0;padding:32px}main{max-width:1500px;margin:auto}h1{font-size:42px}article{background:white;padding:24px;margin:24px 0;border-radius:18px}small{font-size:14px;font-weight:400}p{line-height:1.6}.shots{display:flex;align-items:flex-start;gap:16px}.shots a{width:78%}.shots img{display:block;width:100%;height:auto;border:1px solid #ccd3df}.shots>.mobile{width:22%}@media(max-width:650px){body{padding:14px}.shots{display:block}.shots a,.shots>.mobile{width:100%;margin-bottom:16px}}</style><main><h1>Eight synthetic ventures</h1><p>Mode: <strong>' + html.escape(mode) + '</strong>. Live acceptance: <strong>' + ('PASS' if passed else 'NOT PASSED') + f'</strong>. Provider spend ${spend:.6f}. <a href="report.json">Full evidence and diversity comparisons</a>.</p>' + ''.join(cards) + '</main></html>')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("live", "offline"), required=True)
    parser.add_argument("--runner", help="Trusted module:function; receives input, recent_directions, cap_usd and mode; reserves/meters provider calls")
    parser.add_argument("--replay", type=Path, help="Re-audit existing pipeline bundles without model calls")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/supymem/pagebuilder/gallery")
    parser.add_argument("--budget-usd", type=float, default=MAX_SPEND_USD)
    parser.add_argument("--lighthouse", help="Path to a Lighthouse CLI executable")
    parser.add_argument("--receipts-dir", type=Path, help="Durable metered receipts, including failed builds")
    parser.add_argument("--only", nargs="+", choices=[item["slug"] for item in FIXTURES], help="Run a small synthetic smoke before the complete eight-venture gallery")
    parser.add_argument("--resume", action="store_true", help="Reuse already returned metered bundles and re-run independent browser audits")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after a failure so a systemic issue can be diagnosed before more paid calls")
    args = parser.parse_args()
    if not 0 < args.budget_usd <= MAX_SPEND_USD or not (args.runner or args.replay):
        parser.error("require --runner or --replay and a budget above zero and at most $10")
    runner = None
    if args.runner:
        module, name = args.runner.split(":", 1)
        runner = getattr(importlib.import_module(module), name)
    args.output.mkdir(parents=True, exist_ok=True)
    receipts_dir = args.receipts_dir or (Path(os.environ["COMPANY_OS_DATA_DIR"]) / "pagebuilder-eval-receipts" if os.environ.get("COMPANY_OS_DATA_DIR") else None)
    rows, history, spend = [], [], receipt_spend(receipts_dir) if not args.replay else 0.0
    fixtures = [fixture for fixture in FIXTURES if not args.only or fixture["slug"] in args.only]
    for index, fixture in enumerate(fixtures):
        row = {key: fixture[key] for key in ("slug", "name", "category")}
        row["fixture_version"] = fixture.get("fixture_version", 1)
        destination = args.output / fixture["slug"]
        destination.mkdir(parents=True, exist_ok=True)
        prior_fixture_spend = receipt_spend(receipts_dir, slug=fixture["slug"]) if not args.replay else 0.0
        try:
            started = time.monotonic()
            reuse = args.replay or (args.resume and (destination / "bundle.json").exists())
            if reuse:
                source = args.replay / fixture["slug"] if args.replay else destination
                metadata = json.loads((source / "bundle.json").read_text())
                if source.resolve() != destination.resolve():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                unresolved = receipt_spend(receipts_dir, field="unresolved_ceiling_usd")
                cap = (args.budget_usd - spend - unresolved) / (len(fixtures) - index)
                if cap <= 0:
                    raise ValueError("eval_spend_cap_exhausted")
                generated = runner({**fixture}, recent_directions=history, cap_usd=cap, mode=args.mode)
                if inspect.isawaitable(generated):
                    generated = asyncio.run(generated)
                metadata = write_bundle(destination, generated)
            row["generation_seconds"] = round(time.monotonic() - started, 3) if not reuse else metadata.get("generation_seconds", 0)
            if not reuse:
                metadata["generation_seconds"] = row["generation_seconds"]
                (destination / "bundle.json").write_text(json.dumps(metadata, indent=2, default=str))
            receipt = metadata.get("cost_receipt", metadata.get("receipt", {}))
            row["cost_usd"] = float(receipt.get("provider_cost_usd", receipt.get("cost_usd", 0)))
            if args.replay or receipts_dir is None:
                spend += row["cost_usd"]
            else:
                spend = receipt_spend(receipts_dir)
            if args.mode == "live" and not receipt.get("metered"):
                raise ValueError("live_eval_requires_metered_receipt")
            row["capability_grounding"] = grounding_checks(metadata, fixture)
            if args.mode == "live" and not row["capability_grounding"]["passed"]:
                raise ValueError("live_eval_requires_bound_metered_capability_review")
            backend = receipt.get("waitlist_backend", {})
            row["waitlist_backend"] = backend
            if args.mode == "live" and not (backend.get("configured") and backend.get("local_details_verified") and backend.get("consent_version")):
                raise ValueError("live_eval_requires_waitlist_backend_proof")
            if spend + receipt_spend(receipts_dir, field="unresolved_ceiling_usd") > args.budget_usd + 1e-9:
                raise ValueError("eval_spend_cap_exceeded")
            direction = plain(metadata.get("direction", {}))
            history.append(direction)
            row["direction"] = direction_values(direction)
            waitlist = metadata.get("waitlist_url") or receipt.get("waitlist_url") or metadata.get("manifest", {}).get("waitlist_url") or metadata.get("manifest", {}).get("waitlist", {}).get("url")
            if not waitlist:
                raise ValueError("generated_bundle_missing_waitlist_destination")
            row["bundle_bytes"] = sum(p.stat().st_size for p in destination.rglob("*") if p.is_file() and p.name not in {"bundle.json", "lighthouse.json", "desktop.png", "mobile.png", "hero.png"})
            with serve(destination) as origin:
                row["browser"] = browser_checks(destination, origin, waitlist, require_analytics=args.mode == "live", consent_text=backend.get("consent_text", ""))
                row["hero_phash"] = f"{phash(destination / 'hero.png'):016x}"
                row["source_sha256"] = hashlib.sha256((destination / "index.html").read_bytes()).hexdigest()
                print(json.dumps({"event": "source_ready_for_independent_review", "venture": fixture["slug"],
                                  "source_sha256": row["source_sha256"], "source": str((destination / "index.html").resolve()),
                                  "plan": str((destination / "bundle.json").resolve()),
                                  "desktop": str((destination / "desktop.png").resolve()), "mobile": str((destination / "mobile.png").resolve()),
                                  "hero": str((destination / "hero.png").resolve()),
                                  "assets": [str((destination / asset["path"]).resolve()) for asset in metadata.get("plan", {}).get("assets", [])]}), flush=True)
                row["lighthouse"] = lighthouse(destination, origin, args.lighthouse)
            row["passed"] = row["browser"]["passed"] and row["lighthouse"]["passed"] and row["bundle_bytes"] <= 2 * 1024 * 1024
            if row["passed"]:
                measured = [previous for previous in rows if previous.get("passed") and previous.get("hero_phash")]
                failures = [pair for pair in diversity([*measured, row])["pairs"] if not pair["passed"]]
                if failures:
                    row.update(passed=False, error="eval_visual_diversity_failed", diversity_failures=failures)
        except Exception as exc:
            # Never include provider exception bodies, request payloads or secrets.
            row.update(passed=False, error=str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc)) else type(exc).__name__)
            row["generation_seconds"] = round(time.monotonic() - started, 3)
        if receipts_dir and not args.replay:
            spend = receipt_spend(receipts_dir)
            row["cumulative_fixture_spend_usd"] = round(receipt_spend(receipts_dir, slug=fixture["slug"]), 6)
            if not reuse:
                row["cost_usd"] = round(row["cumulative_fixture_spend_usd"] - prior_fixture_spend, 6)
            copied = args.output / "receipts"
            copied.mkdir(exist_ok=True)
            for receipt in receipts_dir.glob("*.json"):
                shutil.copy2(receipt, copied / receipt.name)
        if not reuse:
            attempts_path = args.output / "attempts.json"
            attempts = json.loads(attempts_path.read_text()) if attempts_path.exists() else []
            attempts.append({"slug": row["slug"], "fixture_version": row["fixture_version"], "at": datetime.now(timezone.utc).isoformat(), "passed": row["passed"],
                             "error": row.get("error"), "cost_usd": row.get("cost_usd", 0), "generation_seconds": row.get("generation_seconds", 0)})
            attempts_path.write_text(json.dumps(attempts, indent=2))
        rows.append(row)
        write_report(args.output, rows, args.mode, spend, args.budget_usd)
        print(json.dumps({"venture": row["slug"], "passed": row["passed"], "error": row.get("error"), "spend_usd": round(spend, 6)}), flush=True)
        if args.fail_fast and not row["passed"]:
            break
    report = write_report(args.output, rows, args.mode, spend, args.budget_usd)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

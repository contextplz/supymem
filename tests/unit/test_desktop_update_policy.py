from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from desktop_runtime.update_policy import (
    apply_policy_to_manifest,
    bake_notes,
    can_defer,
    evaluate_manifest,
    honesty_for_http,
    parse_latest_manifest,
    parse_update_policy,
    rewrite_platform_urls,
    should_force,
)


OPTIONAL_JSON = {
    "version": "0.1.1",
    "notes": bake_notes(False, body="Optional fixture"),
    "platforms": {"darwin-aarch64": {"signature": "x", "url": "http://127.0.0.1/opt.tar.gz"}},
}

FORCE_JSON = {
    "version": "0.1.1",
    "notes": bake_notes(True, min_version="0.1.0", body="Force fixture"),
    "platforms": {"darwin-aarch64": {"signature": "x", "url": "http://127.0.0.1/force.tar.gz"}},
}


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "desktop_update"


def test_optional_allows_defer():
    policy = parse_update_policy(bake_notes(False, body="nice"))
    assert policy["mandatory"] is False
    assert should_force("0.1.0", policy) is False
    assert can_defer(False) is True


def test_mandatory_denies_defer():
    policy = parse_update_policy(bake_notes(True, body="security"))
    assert policy["mandatory"] is True
    assert should_force("0.1.0", policy) is True
    assert can_defer(True) is False


def test_min_version_forces_older_current():
    policy = parse_update_policy(bake_notes(False, min_version="0.2.0", body="floor"))
    assert should_force("0.1.0", policy) is True
    assert should_force("0.2.0", policy) is False
    assert should_force("0.3.1", policy) is False


def test_parse_html_comment_policy():
    notes = '<!--cos-update {"mandatory":true,"min_version":"0.1.0"}-->\nHello'
    policy = parse_update_policy(notes)
    assert policy == {"mandatory": True, "min_version": "0.1.0"}


def test_latest_json_parse_and_optional_branch():
    manifest = parse_latest_manifest(json.dumps(OPTIONAL_JSON))
    result = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", manifest)
    assert result["available"] is True
    assert result["force"] is False
    assert result["can_defer"] is True
    assert result["version"] == "0.1.1"
    assert result["notes"] == "Optional fixture"


def test_latest_json_force_branch():
    manifest = parse_latest_manifest(json.dumps(FORCE_JSON))
    result = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", manifest)
    assert result["available"] is True
    assert result["force"] is True
    assert result["can_defer"] is False


def test_missing_update_is_honest():
    manifest = parse_latest_manifest(json.dumps({"version": "0.1.0", "notes": "same"}))
    result = evaluate_manifest(
        "0.1.0",
        "https://supymem.com/downloads/latest.json",
        manifest,
    )
    assert result["available"] is False
    assert "No newer signed release" in (result["honesty"] or "")
    assert "installed" not in (result["honesty"] or "").lower()


def test_committed_fixtures_match_optional_and_force_branches():
    optional = parse_latest_manifest((FIXTURE_DIR / "latest_optional.json").read_text())
    force = parse_latest_manifest((FIXTURE_DIR / "latest_force.json").read_text())
    opt = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", optional)
    forced = evaluate_manifest("0.1.0", "http://127.0.0.1/latest.json", force)
    assert opt["can_defer"] is True and opt["force"] is False
    assert forced["can_defer"] is False and forced["force"] is True


def test_404_honesty_does_not_claim_install():
    msg = honesty_for_http(404, "http://127.0.0.1:9/latest.json")
    assert "404" in msg
    assert "Nothing was installed" in msg
    assert "local test endpoint" in msg


def test_local_http_fixture_serves_force_and_optional():
    payloads = {"/optional.json": OPTIONAL_JSON, "/force.json": FORCE_JSON, "/missing.json": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = payloads.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"missing")
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        import urllib.error
        import urllib.request

        def fetch(path: str) -> dict:
            url = f"http://{host}:{port}{path}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                manifest = parse_latest_manifest(resp.read().decode())
            return evaluate_manifest("0.1.0", url, manifest)

        optional = fetch("/optional.json")
        assert optional["available"] and optional["can_defer"] and not optional["force"]

        force = fetch("/force.json")
        assert force["available"] and force["force"] and not force["can_defer"]

        missing_url = f"http://{host}:{port}/missing.json"
        try:
            urllib.request.urlopen(missing_url, timeout=3)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            assert "Nothing was installed" in honesty_for_http(exc.code, missing_url)
    finally:
        server.shutdown()


def test_current_version_skips_even_when_mandatory():
    manifest = parse_latest_manifest(json.dumps(FORCE_JSON))
    result = evaluate_manifest("0.1.1", "https://supymem.com/downloads/latest.json", manifest)
    assert result["available"] is False
    assert result["force"] is False
    assert "No newer signed release" in (result["honesty"] or "")


def test_older_version_sees_optional_update():
    manifest = parse_latest_manifest(json.dumps(OPTIONAL_JSON))
    result = evaluate_manifest("0.1.0", "https://supymem.com/downloads/latest.json", manifest)
    assert result["available"] is True
    assert result["force"] is False
    assert result["can_defer"] is True
    assert result["version"] == "0.1.1"


def test_older_version_sees_forced_update():
    manifest = parse_latest_manifest(json.dumps(FORCE_JSON))
    result = evaluate_manifest("0.1.0", "https://supymem.com/downloads/latest.json", manifest)
    assert result["available"] is True
    assert result["force"] is True
    assert result["can_defer"] is False


def test_rewrite_platform_urls_point_at_cloudfront_downloads():
    manifest = {
        "version": "0.1.1",
        "platforms": {
            "darwin-aarch64": {
                "signature": "sig",
                "url": "https://github.com/contextplz/company_os_mvp/releases/download/companyos-v0.1.1/App.tar.gz",
            }
        },
    }
    out = rewrite_platform_urls(manifest, public_base="https://supymem.com/downloads")
    assert out["platforms"]["darwin-aarch64"]["url"] == "https://supymem.com/downloads/App.tar.gz"
    assert out["platforms"]["darwin-aarch64"]["signature"] == "sig"


def test_flip_forced_vs_optional_on_existing_manifest():
    optional = parse_latest_manifest(json.dumps(OPTIONAL_JSON))
    forced = apply_policy_to_manifest(optional, mandatory=True, min_version="0.1.0")
    result = evaluate_manifest("0.1.0", "https://supymem.com/downloads/latest.json", forced)
    assert result["force"] is True
    back = apply_policy_to_manifest(forced, mandatory=False)
    result2 = evaluate_manifest("0.1.0", "https://supymem.com/downloads/latest.json", back)
    assert result2["force"] is False
    assert result2["can_defer"] is True

#!/usr/bin/env python3
"""Export your tool-built page and check local views, consented signups and results.

Run through ~/skills/tq with --api-url, --venture-id, --experiment-id and
--email on a disposable venture. This stores a real consented signup, not a
QA probe. Only loopback HTTP is allowed. No coding tool or browser is started.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class JourneyFailure(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise JourneyFailure(message)


def loopback_url(value):
    parsed = urlsplit(value)
    try:
        valid = (parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback
            and parsed.port is not None and 0 < parsed.port <= 65535
            and not parsed.username and not parsed.password and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    require(valid, "Use an http://127.0.0.1:<port> URL for your local API and page, then rerun.")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise JourneyFailure("Your local page redirected. Rebuild its loopback signup destination on Website, then rerun.")


def request(url, *, body=None, headers=None):
    loopback_url(url)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(url, data=body, headers=headers or {}), timeout=15) as response:
        return response.status, response.read()


def owner_headers():
    from core.local_security import _proxy_secret, console_origin

    return {"X-Local-Proxy-Secret": _proxy_secret().decode("ascii"), "Origin": console_origin()}


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.forms = []
        self.scripts = []
        self.policies = []
        self.primary = None
        self.current_form = None
        self.current_script = None
        self.feed(source)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy":
            self.policies.append({parts[0]: parts[1:] for directive in attrs.get("content", "").split(";")
                if (parts := directive.split())})
        if tag == "a" and attrs.get("id", "").endswith("waitlist_cta"):
            self.primary = attrs.get("href")
        if tag == "form":
            self.current_form = {"attrs": attrs, "inputs": {}}
            self.forms.append(self.current_form)
        if tag == "input" and self.current_form is not None and attrs.get("name") and "disabled" not in attrs:
            self.current_form["inputs"][attrs["name"]] = attrs
        if tag == "script" and attrs.get("data-supymem-analytics") == "1":
            require(not attrs.get("src"), "Ask your coding tool to include your local measurement script on Website, then rerun.")
            self.current_script = ""

    def handle_data(self, data):
        if self.current_script is not None:
            self.current_script += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.current_form = None
        if tag == "script" and self.current_script is not None:
            self.scripts.append(self.current_script)
            self.current_script = None


BEACON_RUNNER = r'''
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const pending = [];
const storage = new Map();
const location = new URL(input.url);
const target = new URL(input.endpoint);
function send(url, options) {
  if (url !== target.href) throw new Error("Unexpected measurement destination");
  pending.push(fetch(url, {...options, headers: {...options.headers,
    Origin: location.origin, Referer: location.href}}).then(response => {
      if (!response.ok) throw new Error("Your local measurement endpoint refused a view. Keep your app running, then rerun.");
  }));
  return Promise.resolve();
}
const document = {referrer: "", querySelectorAll: () => [], addEventListener: () => {},
  querySelector: () => input.primary ? {href: new URL(input.primary, location).href} : null};
const context = {URL, URLSearchParams, Blob, location, document,
  navigator: {webdriver: false, globalPrivacyControl: false, doNotTrack: "0",
    sendBeacon: (url, body) => { send(url, {method: "POST", body}); return true; }},
  sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
  fetch: send, window: {doNotTrack: "0"}};
vm.runInNewContext(input.script, context, {timeout: 1000});
Promise.all(pending).catch(() => {
  console.error("Your local measurement request failed. Keep your app running and check Website, then rerun.");
  process.exitCode = 1;
});
'''


def record_view(source, page_url, endpoint):
    from app.product_analytics.beacon import script_tag

    page = Page(source)
    require(len(page.scripts) == 1, "Ask your coding tool to add your local page measurement on Website, then export again.")
    script = page.scripts[0]
    key = re.search(r'var KEY = ("pk_app_[A-Za-z0-9_-]+")', script)
    require(key is not None and script_tag(key=json.loads(key.group(1)), endpoint=endpoint).strip()
        == '<script data-supymem-analytics="1">' + script + '</script>',
        "Ask your coding tool to update your measurement to match Settings > Features > Page measurement, then export again.")
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    require(len(page.policies) == 1 and "'sha256-" + digest + "'" in page.policies[0].get("script-src", [])
        and endpoint in page.policies[0].get("connect-src", []),
        "Ask your coding tool to allow your measurement script and local endpoint in the page security policy on Website, then export again.")
    # shortcut: check canonical beacons and native forms without a browser; extend the harness for client-only forms.
    subprocess.run(["node", "-e", BEACON_RUNNER], input=json.dumps({"script": script, "url": page_url,
        "endpoint": endpoint, "primary": page.primary}), text=True, check=True, timeout=20)
    return page


def run_journey(api_url, venture_id, experiment_id, email, *, variant, job_id=None, headers=None):
    from app.core.local_mode import is_local
    from core.artifact_policy import extract_zip

    api_url = loopback_url(api_url).rstrip("/")
    require(is_local(), "Run this journey with SUPYMEM_MODE=local and your desktop app's data folder, then rerun.")
    if headers is None:
        headers = owner_headers()
    require(not urlsplit(api_url).query, "Use your local API URL without a query, then rerun.")
    for identifier in (venture_id, experiment_id, variant):
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier), "Choose your saved venture, page test and variant on Website, then rerun.")
    endpoint = api_url + "/e"
    export_url = api_url + f"/ventures/{venture_id}/site/local/export.zip"
    if job_id:
        export_url += "?" + urlencode({"job_id": job_id})
    _, archive = request(export_url, headers=headers)
    with tempfile.TemporaryDirectory(prefix="local-page-signup-") as temporary:
        folder = Path(temporary) / "page"
        extract_zip(archive, folder)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
            "--directory", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            page_url = f"http://127.0.0.1:{port}/index.html?" + urlencode({"experiment_id": experiment_id, "variant": variant})
            deadline = time.monotonic() + 10
            while True:
                try:
                    _, body = request(page_url)
                    break
                except OSError:
                    require(process.poll() is None and time.monotonic() < deadline,
                        "Your loopback page server did not start. Free a local port, then rerun.")
                    time.sleep(.05)
            page = record_view(body.decode("utf-8"), page_url, endpoint)
            _, second = request(page_url)
            require(second == body, "Your exported page changed between visits. Export again from Website, then rerun.")
            record_view(second.decode("utf-8"), page_url, endpoint)
            forms = [form for form in page.forms if "email" in form["inputs"]]
            require(len(forms) == 1, "Ask your coding tool to add one accessible waitlist form on your page in Website, then rerun.")
            form = forms[0]
            action = loopback_url(urljoin(page_url, form["attrs"].get("action", "")))
            require(action == api_url + f"/public/waitlists/{venture_id}" and form["attrs"].get("method", "get").lower() == "post"
                and urlsplit(action).scheme + "://" + urlsplit(action).netloc in page.policies[0].get("form-action", []),
                "Ask your coding tool to use your exact local waitlist destination and form security policy on Website, then export again.")
            consent = form["inputs"].get("consent", {})
            require(consent.get("type") == "checkbox" and "required" in consent and "checked" not in consent,
                "Ask your coding tool to add required unchecked signup consent on Website, then export again.")
            payload = {name: field.get("value", "") for name, field in form["inputs"].items() if field.get("type") == "hidden"}
            payload.update(email=email, consent=consent.get("value", ""), website="")
            require(payload.get("experiment_id") == experiment_id and payload.get("variant") == variant,
                "Ask your coding tool to keep your page test and variant in the signup form on Website, then export again.")
            status, _ = request(action, body=urlencode(payload).encode(), headers={"Content-Type": "application/x-www-form-urlencoded",
                "Origin": urlsplit(page_url).scheme + "://" + urlsplit(page_url).netloc, "Referer": page_url, "Accept": "application/json"})
            require(status == 201, "Open Website > Collect signups and check your waitlist, then rerun.")
        finally:
            process.terminate()
            process.wait(timeout=10)
    request(api_url + f"/ventures/{venture_id}/chat/confirm", body=json.dumps({"action": "experiment.page.readout",
        "payload": {"experiment_id": experiment_id}}).encode(), headers={**(headers or {}), "Content-Type": "application/json"})
    _, body = request(api_url + f"/ventures/{venture_id}/experiments", headers=headers)
    experiment = next((item for item in json.loads(body)["items"] if item["id"] == experiment_id), None)
    require(experiment and "1/2 signups/views" in experiment.get("learning", ""),
        "Open your page test in Experiments and refresh your results. Check its signup and view attribution on Website, then rerun.")
    return {"signups": 1, "views": 2, "experiment_id": experiment_id, "outcome": experiment["outcome"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("api-url", "venture-id", "experiment-id", "email"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    try:
        result = run_journey(args.api_url, args.venture_id, args.experiment_id, args.email,
            variant=args.variant, job_id=args.job_id)
    except JourneyFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.SubprocessError):
        print("Your local signup journey did not finish. Open Website and Experiments, check your page destination and measurement, keep your app running, then rerun.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

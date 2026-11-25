"""Descriptive CU task metadata, independent of execution routing and authority."""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

CuTaskClass = Literal["native_app", "browser_logged_in", "browser_public"]

_BROWSER_APPS = {"browser", "chrome", "google chrome", "safari", "firefox", "edge", "microsoft edge", "arc", "brave",
                 "com.google.chrome", "com.apple.safari", "org.mozilla.firefox", "com.microsoft.edgemac",
                 "company.thebrowser.browser", "com.brave.browser", "brave browser"}
_WEB_APPS = {"gmail", "google calendar", "google docs", "google sheets", "google drive", "linkedin"}
_NATIVE_TAGS = {"native_app", "desktop", "gui", "notes", "calendar", "mail", "finder", "numbers",
                "preview", "reminders", "textedit", "cross-app", "cross_app", "multiwindow"}
_SESSION_TAGS = {"browser_logged_in", "logged_in", "signed_in", "authenticated", "requires_login"}
_PRIVATE_HOSTS = {"mail.google.com", "calendar.google.com", "drive.google.com", "docs.google.com",
                  "outlook.live.com", "outlook.office.com", "outlook.office365.com"}


def classify_cu_task(task: dict) -> tuple[CuTaskClass, str]:
    """Classify declared app/URL/step/tag intent without inspecting live sessions.

    This is metadata only: all tasks continue to run on the Mac. browser_public
    tasks are candidates for a future cloud runner, not grants to execute there.
    Native requirements win over browser requirements; unclear targets or session
    requirements conservatively retain a Mac/session-dependent classification.
    """
    # ponytail: declaration heuristics cannot prove anonymous access, validate it before any future cloud dispatch.
    manifest = task.get("manifest") or {}
    steps = task.get("steps") or manifest.get("steps") or []
    records = [task, manifest] + ([step if isinstance(step, dict) else {"instruction": step}
                                 for step in steps if isinstance(step, (dict, str))] if isinstance(steps, list) else [])
    apps, tags, urls, actions, goals = [], set(), [], [], []
    for record in records:
        for key, destination in (("target_app", apps), ("app", apps), ("applications", apps), ("application_bundle", apps),
                                 ("url", urls), ("urls", urls), ("origins", urls),
                                 ("actions", actions), ("op", actions)):
            value = record.get(key)
            destination.extend([value] if isinstance(value, str) else
                               [item for item in value if isinstance(item, str)] if isinstance(value, list) else [])
        for key in ("tags", "category", "backend"):
            value = record.get(key)
            tags.update([value.lower()] if isinstance(value, str) else
                        [item.lower() for item in value if isinstance(item, str)] if isinstance(value, list) else [])
        for key in ("instruction", "prompt", "task", "goal", "text", "description"):
            if isinstance(record.get(key), str):
                goal = record[key]
                if record is manifest:
                    goal = goal.split("\nReviewed browser policy:", 1)[0]
                    if "work toward: " in goal and ". Stay on these exact sites only:" in goal:
                        goal = goal.split("work toward: ", 1)[1].split(". Stay on these exact sites only:", 1)[0]
                goals.append(goal)
    desktop = manifest.get("desktop_verification") or task.get("desktop_verification")
    if desktop or tags & _NATIVE_TAGS or any(action.startswith("desktop.") for action in actions):
        return "native_app", "native_scope"
    normalized_apps = {app.strip().lower() for app in apps}
    if normalized_apps - _BROWSER_APPS - _WEB_APPS:
        return "native_app", "native_target"
    goal = " ".join(goals).lower()
    if re.search(r"\bcross[- ]app\b", goal):
        return "native_app", "cross_app"
    if re.search(r"\b(?:open|launch|in|using|with|into|to) (?:apple |macos )?(?:notes|calendar|mail|finder|numbers|preview|reminders|textedit)\b", goal):
        return "native_app", "native_goal"
    browser = manifest.get("general_browser_verification") or task.get("general_browser_verification") or {}
    verification = manifest.get("browser_verification") or task.get("browser_verification") or {}
    urls.extend(value for value in (browser.get("start_url"), verification.get("url")) if isinstance(value, str))
    urls.extend(value for value in browser.get("read_urls", []) if isinstance(value, str))
    urls.extend(value for value in verification.get("expected_locations", []) if isinstance(value, str))
    urls.extend(re.findall(r"https?://[^\s<>\"']+", goal))
    web_goal = re.search(r"\b(gmail|linkedin|google (?:calendar|docs|sheets|drive))\b", goal)
    has_browser = bool(urls or normalized_apps or web_goal or "browser" in tags or "browser_public" in tags
                       or any(action.startswith("browser.") for action in actions))
    if not has_browser and re.search(r"\bcalendar\b", goal):
        return "native_app", "native_goal"
    session_goal = re.sub(r"\b(?:no|without|do not|don't) (?:log[ -]?in|sign[ -]?in|cookies)\b", "", goal)
    if (tags & _SESSION_TAGS or any(record.get(key) is True for record in records
            for key in ("requires_login", "requires_session", "requires_cookies"))
            or re.search(r"\b(logged[- ]in|signed[- ]in|sign[ -]?in|log[ -]?in|cookies|authenticated|my account|(?:existing|signed[- ]in|browser) sessions?)\b", session_goal)):
        return "browser_logged_in", "session_required"
    public_urls = False
    account_site = False
    for url in urls:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        public_urls |= parsed.scheme in {"http", "https"} and bool(host)
        account_site |= host in {"linkedin.com", "www.linkedin.com", "x.com", "twitter.com", "github.com"}
        if host in _PRIVATE_HOSTS or re.search(r"^/(?:feed|messaging|inbox|dashboard|settings|account|compose|login|signin)(?:/|$)", parsed.path.lower()):
            return "browser_logged_in", "account_surface"
    if (normalized_apps & _WEB_APPS or web_goal or account_site) and re.search(
                r"\b(post|publish|reply|send|schedule|book|edit|upload|delete)\b", goal):
        return "browser_logged_in", "account_action"
    if re.search(r"\b(?:post|publish|reply) (?:on|to|in)\b", goal):
        return "browser_logged_in", "account_action"
    if re.search(r"\b(my|our) (?:gmail |google )?(?:inbox|messages|profile|dashboard|calendar|drive)\b", goal):
        return "browser_logged_in", "account_action"
    if public_urls:
        return "browser_public", "public_url"
    if "browser_public" in tags:
        return "browser_public", "public_tag"
    if has_browser:
        return "browser_logged_in", "unknown_session"
    return "native_app", "unknown_target"

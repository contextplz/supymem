"""Declaration-only classification; no desktop, network, or routing changes."""
from copy import deepcopy

import pytest

from core.cu_task_classification import classify_cu_task


@pytest.mark.parametrize("task, expected, reason", [
    ({"target_app": "Calendar", "steps": ["Book a calendar slot for the team tomorrow at 2pm"]},
     "native_app", "native_target"),
    ({"target_app": "Chrome", "url": "https://acme.example/contact", "steps": [
        "Fill the public contact form with my name and work email, no login"]}, "browser_public", "public_url"),
    ({"target_app": "Numbers", "steps": ["Export the revenue sheet as a CSV"]}, "native_app", "native_target"),
    ({"url": "https://www.linkedin.com/feed/", "prompt": "Post our launch on LinkedIn while logged in"},
     "browser_logged_in", "session_required"),
    ({"url": "https://stripe.com/pricing", "prompt": "Read the public pricing page"}, "browser_public", "public_url"),
    ({"steps": [{"backend": "browser", "url": "https://linear.app/pricing"},
                {"app": "Safari", "url": "https://notion.so/pricing"}], "prompt": "Compare two public SaaS sites"},
     "browser_public", "public_url"),
    ({"target_app": "Gmail", "url": "https://mail.google.com/mail/u/0/#inbox", "steps": ["Reply to the supplier"]},
     "browser_logged_in", "account_surface"),
    ({"steps": [{"app": "Notes"}, {"backend": "browser", "url": "https://example.com"}]},
     "native_app", "native_target"),
    ({"tags": ["cross-app"], "url": "https://example.com"}, "native_app", "native_scope"),
    ({"steps": [{"backend": "gui", "url": "https://example.com"}]}, "native_app", "native_scope"),
    ({"manifest": {"applications": ["com.apple.TextEdit"], "actions": ["desktop.read", "desktop.interact"]}},
     "native_app", "native_scope"),
    ({"url": "https://example.com", "steps": [{"tags": ["signed_in"]}]}, "browser_logged_in", "session_required"),
    ({"url": "https://example.com", "requires_cookies": True}, "browser_logged_in", "session_required"),
    ({"url": "https://example.com", "prompt": "Use the founder's existing Chrome browser session"},
     "browser_logged_in", "session_required"),
    ({"url": "https://example.com/dashboard", "prompt": "Read my metrics"}, "browser_logged_in", "account_surface"),
    ({"url": "https://linkedin.com", "steps": ["Post our launch on LinkedIn"]}, "browser_logged_in", "account_action"),
    ({"url": "https://www.linkedin.com", "steps": ["Publish the launch update"]}, "browser_logged_in", "account_action"),
    ({"url": "https://linkedin.com/company/acme", "prompt": "Read the public company page"},
     "browser_public", "public_url"),
    ({"prompt": "Reply in Gmail"}, "browser_logged_in", "account_action"),
    ({"prompt": "Book a slot in Google Calendar"}, "browser_logged_in", "account_action"),
    ({"prompt": "Read https://example.com/pricing and put the results in Numbers"}, "native_app", "native_goal"),
    ({"prompt": "Read https://example.com and copy the results into Notes"}, "native_app", "native_goal"),
    ({"target_app": "Brave", "url": "https://example.com"}, "browser_public", "public_url"),
    ({"manifest": {"applications": ["com.google.Chrome"], "actions": ["browser.read"],
                   "general_browser_verification": {"start_url": "https://example.com/pricing"}}},
     "browser_public", "public_url"),
    ({"manifest": {"actions": ["browser.read"], "browser_verification": {"expected_locations": ["https://example.com"]}}},
     "browser_public", "public_url"),
    ({"prompt": "Read my Gmail inbox"}, "browser_logged_in", "account_action"),
    ({"url": "https://x.com", "prompt": "Post on X"}, "browser_logged_in", "account_action"),
    ({"manifest": {"instruction": "Read https://example.com/pricing\nReviewed browser policy: passwords, sign in, cookies require handoff.",
                   "origins": ["https://example.com"]}}, "browser_public", "public_url"),
    ({"manifest": {"instruction": "In the private browser work toward: Fill the public contact form. Stay on these exact sites only: "
                   "https://example.com. Do not use the founder's personal Chrome profile or sign in.",
                   "origins": ["https://example.com"]}}, "browser_public", "public_url"),
    ({"target_app": "Chrome"}, "browser_logged_in", "unknown_session"),
    ({"url": "https://[broken"}, "browser_logged_in", "unknown_session"),
    ({"tags": ["browser_public"]}, "browser_public", "public_tag"),
    ({"steps": None, "tags": None, "manifest": None}, "native_app", "unknown_target"),
])
def test_classification_table_is_pure(task, expected, reason):
    original = deepcopy(task)
    assert classify_cu_task(task) == (expected, reason)
    assert task == original


@pytest.mark.parametrize("app", ["Notes", "Calendar", "Mail", "Finder", "Numbers", "Preview", "Reminders", "TextEdit"])
def test_native_targets_win_over_session_requirements(app):
    assert classify_cu_task({"target_app": app, "tags": ["logged_in"], "url": "https://mail.google.com"})[0] == "native_app"

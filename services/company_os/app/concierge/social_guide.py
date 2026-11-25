"""Click-by-click Zernio + X connect walkthrough for non-technical founders.

Screens described from Zernio's live signup (zernio.com/signup) and docs
(docs.zernio.com Quickstart + Connecting Accounts, retrieved 2026-08-21).
Zernio is the pre-approved posting app. The founder never creates an X developer
app and never pastes an X password into Company OS.
"""

from __future__ import annotations

import re
from typing import Any

# Docs: sk_ + 64 hex chars. Also accept zn_ leftovers from older copy.
ZERNIO_KEY_RE = re.compile(r"\b((?:sk_|zn_)[A-Za-z0-9_-]{16,})\b")


def extract_zernio_key(text: str) -> str | None:
    match = ZERNIO_KEY_RE.search(text or "")
    return match.group(1) if match else None


def infer_step_id(text: str) -> str | None:
    t = (text or "").lower()
    if "api key" in t or "sk_" in t or "paste" in t:
        return "api_key"
    if "sign up" in t or "signup" in t or "create" in t and "account" in t:
        return "signup"
    if re.search(r"\b(twitter|x\b|linkedin|instagram)\b", t):
        return "connect_x"
    return None


WALKTHROUGH: list[dict[str, Any]] = [
    {
        "id": "signup",
        "title": "Step 1, Make a free Zernio account",
        "summary": "Zernio is a helper website. It already has permission from X, LinkedIn, and Instagram to post for apps like ours. You are not building a Twitter developer app.",
        "url": "https://zernio.com/signup",
        "open_label": "Open Zernio sign-up",
        "look_for": [
            "A large heading that says Get started",
            "A line under it: Free forever. No credit card.",
            "Two big buttons stacked or side by side: Continue with Google and Continue with GitHub",
            "Smaller text: Use email and password instead",
            "At the bottom: Already have an account? Sign in",
        ],
        "cursor": "Move to the upper-middle of the white form, onto the first large button labelled Continue with Google (use Continue with GitHub if that is how you usually sign in).",
        "click": "Click Continue with Google. A Google window or full page appears. Click the Gmail address you want. If Google asks Allow, click Allow. You will not be asked for a credit card.",
        "expected_after": "You land inside Zernio, usually a dashboard with a menu on the left. If you prefer email: click Use email and password instead, type an email and a new password, then click Create account.",
        "keep_in_mind": [
            "If you already signed up, click Already have an account? Sign in at the bottom instead.",
            "This page is Zernio, not X. Do not type your X / Twitter password here.",
            "The first two social accounts you connect later are free forever.",
        ],
    },
    {
        "id": "connect_x",
        "title": "Step 2, Connect your X (Twitter) account",
        "summary": "You log in on X's own website. X then tells Zernio it may post for you. We never see your X password.",
        "url": "https://zernio.com/dashboard",
        "open_label": "Open Zernio dashboard",
        "look_for": [
            "A left-hand menu (Accounts, Profiles, Settings, or similar)",
            "A list or grid of social networks with logos (X / Twitter, LinkedIn, Instagram…)",
            "A button or link next to X that says Connect, Add, or + ",
        ],
        "cursor": "On the dashboard, look at the left edge first. If you see Accounts or Social accounts, click that. Then move to the X (formerly Twitter) bird / X logo in the list. The Connect control is immediately to the right of that logo, or under it on a card.",
        "click": "Click Connect (or Add) on X. A new tab or window opens on x.com, that is Twitter's real site. If you are not logged in, sign in to X there. Then X shows a permission screen listing things like post tweets. Click Authorize or Allow (usually a black or blue button at the bottom of that card).",
        "expected_after": "The extra window closes or returns you to Zernio. Next to X you should see Connected, a green tick, or your @handle. If X asks which account and you have more than one, pick the one you want the company to tweet from.",
        "keep_in_mind": [
            "Stay on x.com for the password. If a site other than x.com or twitter.com asks for your X password, cancel.",
            "You do not need developer.x.com and you do not need an X API key. Zernio already has that.",
            "LinkedIn and Instagram are the same idea: Connect on the dashboard, then authorise on LinkedIn or Instagram's own page. Optional for a first test, X alone is enough.",
        ],
    },
    {
        "id": "api_key",
        "title": "Step 3, Copy a Zernio API key (shown only once)",
        "summary": "This key is a long secret that lets Company OS talk to Zernio. It is not your X password.",
        "url": "https://zernio.com/dashboard/api-keys",
        "open_label": "Open API keys",
        "look_for": [
            "Settings in the left menu, then API Keys, or a page titled API Keys",
            "A button labelled Create API Key or New API key (usually top-right of the main white area)",
            "After you click it: a long string starting with sk_ in a box, plus a Copy button",
        ],
        "cursor": "If you are not already on the API Keys page, click Settings at the bottom of the left menu, then API Keys. Move to the top-right of the main panel onto Create API Key.",
        "click": "Click Create API Key. The new key appears immediately. Click Copy (often a small overlapping-squares icon on the right of the key). Then come back to this Company OS page and paste it in the box labelled Zernio API key below.",
        "expected_after": "The key looks like sk_ followed by a long jumble of letters and numbers. Paste the whole thing. After you leave this Zernio screen you usually cannot see the same key again, if you lose it, create a new one and paste the new one here.",
        "keep_in_mind": [
            "Copy it now. Zernio hashes keys and will not show this one again.",
            "Never paste this into a public tweet, email, or screenshot.",
            "Company OS stores it in a local vault. Chat will not repeat the full key back to you.",
            "If the page 404s, open the dashboard, click Settings, then look for API Keys in that settings list.",
        ],
    },
    {
        "id": "paste",
        "title": "Step 4, Paste the key here and save",
        "summary": "Only the Zernio key goes in the form on this page. Optional: your public @handle so drafts know the brand name.",
        "url": None,
        "open_label": None,
        "look_for": [
            "The box on this page labelled Zernio API key",
            "Optional boxes for X / Twitter, Instagram, LinkedIn (public names only)",
            "The green or dark button Save and continue at the bottom",
        ],
        "cursor": "Scroll down this same Company OS page until you see Zernio API key. Click inside that box so the cursor blinks there.",
        "click": "Paste (Ctrl+V or Cmd+V). You can also type your public X handle like @yourname in the X / Twitter box, not a password. Then click Save and continue.",
        "expected_after": "This page says Saved and takes you back to the cockpit. Parked social tasks can run. You can also tell chat: post this to twitter: hello from a test.",
        "keep_in_mind": [
            "If posting still fails after save, chat: posting isn't working, the team will check whether X is connected inside Zernio.",
            "You can skip Instagram and LinkedIn for the first test.",
        ],
    },
]


SOCIAL_GUIDE: dict[str, Any] = {
    "open_url": "https://zernio.com/signup",
    "open_label": "Start: open Zernio sign-up",
    "skip_label": "Skip posting for now",
    "steps": [
        {
            "text": "Make a free Zernio account (Google or GitHub, no credit card)",
            "url": "https://zernio.com/signup",
        },
        {
            "text": "In the Zernio dashboard, connect X (you log in on x.com, not here)",
            "url": "https://zernio.com/dashboard",
        },
        {
            "text": "Create an API key (starts with sk_) and copy it immediately",
            "url": "https://zernio.com/dashboard/api-keys",
        },
        {
            "text": "Paste that key in the box below. Never paste your X password.",
        },
    ],
    "fields": [
        {
            "id": "token",
            "label": "Zernio API key (starts with sk_)",
            "secret": True,
            "placeholder": "sk_…",
            "optional": True,
        },
        {"id": "x", "label": "Your public X / Twitter handle (optional)", "placeholder": "@you", "optional": True},
        {"id": "instagram", "label": "Instagram (optional)", "placeholder": "@you", "optional": True},
        {
            "id": "linkedin",
            "label": "LinkedIn URL (optional)",
            "placeholder": "linkedin.com/in/…",
            "optional": True,
        },
    ],
    "extra_links": [
        {"label": "Zernio dashboard", "url": "https://zernio.com/dashboard"},
        {"label": "API keys", "url": "https://zernio.com/dashboard/api-keys"},
        {"label": "X login (only when Zernio sends you there)", "url": "https://x.com/i/flow/login"},
    ],
    "walkthrough": WALKTHROUGH,
    "chat_hints": [
        "Explain how to connect X",
        "Posting isn't working",
        "I have a Zernio API key",
    ],
}


def walkthrough_markdown(*, step_id: str | None = None) -> str:
    """Founder-facing markdown the chatbot can paste into chat."""
    steps = WALKTHROUGH
    if step_id:
        steps = [row for row in WALKTHROUGH if row["id"] == step_id] or WALKTHROUGH
    parts = [
        "You do **not** need an X developer account or an X API secret. "
        "Zernio already has that. You only sign in to X to say “yes, this app may post.”",
        "",
    ]
    for row in steps:
        parts.append(f"### {row['title']}")
        parts.append(row["summary"])
        if row.get("url"):
            parts.append(f"Open: {row['url']}")
        parts.append("**What you should see**")
        for line in row.get("look_for") or []:
            parts.append(f"- {line}")
        parts.append(f"**Where to move the cursor:** {row['cursor']}")
        parts.append(f"**What to click:** {row['click']}")
        parts.append(f"**What happens next:** {row['expected_after']}")
        keep = row.get("keep_in_mind") or []
        if keep:
            parts.append("**Keep in mind**")
            for line in keep:
                parts.append(f"- {line}")
        parts.append("")
    parts.append(
        "When you have the `sk_` key, paste it in Connect posting, or paste it in this chat "
        "and confirm so I can save it. I will not repeat the full key."
    )
    return "\n".join(parts).strip()

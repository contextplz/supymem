"""Supymem email: one branded layout (600px, table-based, inline CSS, dark-mode aware) and Charlie's copy.

Every component returns ``(html, text)`` so the plain-text alternative is built from the same content.
Every piece of user-influenced data goes through ``escape``; URLs are built from our own base + a checked path.
"""
from __future__ import annotations

import os
import re
from html import escape
from urllib.parse import parse_qs, quote, unquote, urlsplit

from core.email_theme import (
    BG, BORDER, BRAND, BRAND_DARK_BG, CARD, CSS as _CSS, FONT, INFO_BG, INK,
    MUTED, OK, OK_BG, RADIUS, RADIUS_CONTROL, RADIUS_SM, SURFACE, TEXT, WARN,
    WARN_BG, _base_url, app_url, logo_header,
)

TRANSACTIONAL = "transactional"
CATEGORIES = ("product_updates", "engagement_reminders", "digests", "suggestions")
ADDRESS_REQUIRED = ("beta_invite",)  # transactional-category mail that still goes to people who never signed up
DEFAULT_ADDRESS = "Supymem"  # service.deliver refuses marketing-type mail on a real provider until EMAIL_PHYSICAL_ADDRESS is set

Block = tuple[str, str]  # (html, text)


def _beta_download_url(code: str) -> str:
    return _base_url("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com") + "/public/beta/download?code=" + quote(code, safe="")


def _admission_download_url(ctx: dict) -> str:
    code = ctx.get("admission_code")
    return _beta_download_url(code.upper()) if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9]{8}", code) else ""


def _path(ctx: dict) -> str:
    p = str(ctx.get("path") or "/")
    decoded = unquote(unquote(p))
    return p if decoded.startswith("/") and not decoded.startswith("//") and "\\" not in decoded and not any(ord(c) < 32 for c in decoded) else "/"


def _chat_url(ctx: dict, path: str) -> str:
    """Venture CTAs open the existing chat panel; account/download/recovery links stay intact."""
    from app.chat.navigation_map import hidden_href

    path = _path({"path": path})
    parsed = urlsplit(unquote(unquote(path)))
    venture_path = re.match(r"^/v/([^/]+)(?:/|$)", parsed.path)
    hidden = hidden_href(parsed.path)
    if venture_path or hidden:
        venture_id = (ctx.get("_venture_id") or ctx.get("venture_id")
                      or (venture_path[1] if venture_path else parse_qs(parsed.query).get("venture", [""])[0]))
        if isinstance(venture_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", venture_id):
            # ponytail: chat=1 only opens the panel, add conversation/prefill queries when the console supports them.
            return app_url() + f"/v/{quote(venture_id, safe='')}?chat=1"
        return app_url() + "/"
    return app_url() + path


def _items(ctx: dict) -> list[str]:
    values = ctx.get("items") or []
    return [_clean(i, 240) for i in values[:8]] if isinstance(values, (list, tuple)) else []


def _clean(value, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _name(ctx: dict) -> str:
    name = _clean(ctx.get("venture_name"), 80)
    if not name or re.match(r"^(hello\b|hi\b|untitled\b|new venture\b|my idea\b|i (want|would|have)\b)", name, re.I):
        return "your venture"
    return name


def _first(ctx: dict) -> str:
    return _clean(ctx.get("first_name"), 40)


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


# ------------------------------------------------------------ components
def paragraph(text: str) -> Block:
    return (f'<p class="txt" style="margin:0 0 16px;font-size:16px;line-height:1.6;color:{TEXT}">{escape(text)}</p>', text)


def button(label: str, url: str) -> Block:
    html = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr>'
            f'<td class="btn" bgcolor="{BRAND}" style="border-radius:{RADIUS_CONTROL};background:{BRAND};mso-padding-alt:14px 24px">'
            f'<a class="primary-cta" href="{escape(url)}" style="display:inline-block;padding:14px 24px;font-size:16px;font-weight:700;'
            f'color:{INK};text-decoration:none;border-radius:{RADIUS_CONTROL};font-family:{FONT};line-height:1.4">{escape(label)}</a></td></tr></table>')
    return html, f"{label}: {url}"


_TONES = {"info": ("c-info", INFO_BG, BRAND), "warn": ("c-warn", WARN_BG, WARN), "ok": ("c-ok", OK_BG, OK)}


def callout(text: str, tone: str = "info", title: str = "") -> Block:
    cls, bg, edge = _TONES[tone]
    head = f'<div class="txt" style="font-weight:700;font-size:15px;margin:0 0 4px;color:{TEXT}">{escape(title)}</div>' if title else ""
    html = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px"><tr>'
            f'<td class="{cls}" bgcolor="{bg}" style="background:{bg};border-left:4px solid {edge};border-radius:{RADIUS_SM};padding:14px 16px">'
            f'{head}<div class="txt" style="font-size:15px;line-height:1.55;color:{TEXT}">{escape(text)}</div></td></tr></table>')
    return html, (f"{title}\n" if title else "") + text


def stat_row(stats: list[tuple[str, str]]) -> Block:
    stats = stats[:3]
    width = 100 // max(len(stats), 1)
    cells = "".join(
        f'<td class="stat" width="{width}%" align="center" style="padding:14px 6px;background:{SURFACE};border-radius:{RADIUS_SM}">'
        f'<div class="accent" style="font-size:28px;font-weight:800;line-height:1.1;color:{BRAND}">{escape(str(v))}</div>'
        f'<div class="muted" style="font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:{MUTED};margin-top:4px">{escape(label)}</div></td>'
        for v, label in stats)
    html = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="6" style="margin:0 0 18px"><tr>{cells}</tr></table>'
    return html, "  ".join(f"{v} {label}" for v, label in stats)


def item_list(items: list[str]) -> Block:
    rows = "".join(
        f'<tr><td valign="top" style="padding:0 10px 10px 0;color:{BRAND};font-size:16px;line-height:1.5">&bull;</td>'
        f'<td class="txt" style="padding:0 0 10px;font-size:15px;line-height:1.5;color:{TEXT}">{escape(i)}</td></tr>' for i in items)
    return f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 18px">{rows}</table>', "\n".join("- " + i for i in items)


def timeline(steps: list[tuple[str, str]]) -> Block:
    """steps: (label, state) with state in done|current|todo."""
    mark = {"done": ("&#10003;", BRAND, INK), "current": ("&bull;", CARD, BRAND), "todo": ("", SURFACE, SURFACE)}
    glyph = {"done": "[x]", "current": "[>]", "todo": "[ ]"}
    rows = []
    for label, state in steps:
        sym, bg, fg = mark[state]
        border = f"2px solid {BRAND}" if state == "current" else f"2px solid {bg}"
        weight = "700" if state == "current" else "400"
        rows.append(
            f'<tr><td width="30" valign="middle" style="padding:0 0 10px"><div class="dot-{state}" style="width:22px;height:22px;line-height:22px;'
            f'border-radius:11px;text-align:center;font-size:13px;font-weight:700;background:{bg};color:{fg};border:{border}">{sym}</div></td>'
            f'<td class="txt" valign="middle" style="padding:0 0 10px;font-size:15px;font-weight:{weight};color:{TEXT}">{escape(label)}</td></tr>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px">{"".join(rows)}</table>',
            "\n".join(f"{glyph[s]} {label}" for label, s in steps))


def links(pairs: list[tuple[str, str]]) -> Block:
    html = (f'<p class="muted" style="margin:0 0 6px;font-size:14px;color:{MUTED}">'
            + " &nbsp;&middot;&nbsp; ".join(f'<a href="{escape(u)}" style="color:{BRAND};text-decoration:underline">{escape(t)}</a>' for t, u in pairs) + "</p>")
    return html, "\n".join(f"{t}: {u}" for t, u in pairs)


# ---------------------------------------------------------------- layout
def _layout(head: str, blocks: list[Block], *, pre: str, address: str, unsubscribe_url: str, prefs_url: str, why: str = "") -> Block:
    body = "".join(h for h, _ in blocks)
    foot_links = ""
    if unsubscribe_url:
        foot_links = (f'<a href="{escape(unsubscribe_url)}" style="color:{MUTED};text-decoration:underline">Unsubscribe from these emails</a>'
                      f' &nbsp;&middot;&nbsp; <a href="{escape(prefs_url)}" style="color:{MUTED};text-decoration:underline">Email settings</a><br>')
    reason = why or ("You're getting this because you have a Supymem account." if not unsubscribe_url else "You're getting this because you use Supymem.")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>{escape(head)}</title><style>{_CSS}</style></head>
<body class="bg" style="margin:0;padding:0;background:{BG};font-family:{FONT}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all">{escape(pre)}{"&zwnj;&nbsp;" * 40}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="bg" style="background:{BG}"><tr><td class="outer" align="center" style="padding:36px 12px">
<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
<tr><td class="pad" style="padding:0 4px 18px">{logo_header()}</td></tr>
<tr><td class="card" bgcolor="{CARD}" style="background:{CARD};border:1px solid {BORDER};border-radius:{RADIUS}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td class="pad" style="padding:36px 36px 12px">
<p class="muted" style="margin:0 0 12px;font-size:12px;line-height:1.4;letter-spacing:.08em;text-transform:uppercase;color:{MUTED}">A note from Charlie</p>
<h1 class="txt" style="margin:0 0 20px;font-size:30px;line-height:1.2;font-weight:800;letter-spacing:-.025em;color:{TEXT};font-family:{FONT}">{escape(head)}</h1>
{body}
<p class="txt" style="margin:20px 0 24px;font-size:15px;line-height:1.5;color:{TEXT}"><strong>, Charlie</strong><br><span class="muted" style="font-size:13px;color:{MUTED}">Your Supymem manager</span></p>
</td></tr></table></td></tr>
<tr><td class="pad foot muted" style="padding:20px 8px 0;font-size:12px;line-height:1.7;color:{MUTED}">{foot_links}{escape(reason)}<br>{escape(address)}</td></tr>
</table><!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>"""
    text = "\n\n".join([head, *(t for _, t in blocks if t), "Charlie\nYour Supymem manager"])
    text += "\n\n--\n" + (f"Unsubscribe: {unsubscribe_url}\nEmail settings: {prefs_url}\n" if unsubscribe_url else "") + address
    return html, text


# --------------------------------------------------------------- content
def _hi(c: dict) -> list[Block]:
    f = _first(c)
    return [paragraph(f"Hi {f},")] if f else []


def _note(c, subject, line, cta, *, note="", after=None, path=None):
    return dict(subject=subject, pre=line, head=subject,
                blocks=[*_hi(c), paragraph(line), *([paragraph(note)] if note else [])],
                cta=cta, path=_path(c) if path is None else path, after=after or [])


def _welcome(c):
    f = _first(c)
    d = _note(c, "Welcome to Supymem", "Tell Charlie your idea in a sentence.", "Start your first venture",
              note="I'll research it, draft a plan and help you build. You make the decisions.", path="/new",
              after=[timeline([("Tell me your idea", "current"), ("Review the research", "todo"), ("Choose your next step", "todo")])])
    d["head"] = f"Welcome, {f}." if f else "Welcome. I'm Charlie."
    if f:
        d["blocks"] = d["blocks"][1:]  # the heading already greets them
    download = _admission_download_url(c) if c.get("web_signup") else ""
    if download:
        d["after"].append(links([("Get Supymem for Mac", download)]))
    return d


_MILESTONE = {
    "research": ("Your research is ready", "The research for {name} is ready to review.", "Read full report"),
    "plan": ("Your plan is ready", "The plan for {name} is ready for your review.", "Review your plan"),
    "landing": ("Your landing page is live", "The landing page for {name} is published and ready to share.", "Get your first signups"),
    "waitlist": ("Your first waitlist signup", "Someone joined the waitlist for {name}.", "View your waitlist"),
    "build": ("Your build was accepted", "The build for {name} was accepted and is ready to review.", "Review your build"),
}


def _milestone(c):
    what = str(c.get("what"))
    subject, line, cta = _MILESTONE.get(what, ("Your venture has an update", "There's an update for {name}.", "View the update"))
    after = []
    note = ""
    if what == "waitlist":
        n = _count(c.get("count"))
        if n > 1:
            subject, line = "Your waitlist is growing", f"{n} people joined the waitlist for {{name}}."
        note = "These signups are unverified interest, not proof of demand or revenue."
    elif what == "research":
        verdict = _clean(c.get("verdict"), 120)
        if verdict:
            after.append(callout(verdict, "info", "Research verdict"))
        if _items(c):
            after.append(item_list(_items(c)[:3]))
    elif what == "landing":
        note = "Share it with the people you built it for."
        url = _clean(c.get("url"), 500)
        try:
            parsed = urlsplit(url)
        except ValueError:
            parsed = urlsplit("")
        if parsed.scheme == "https" and parsed.netloc and not parsed.username:
            after.append(paragraph(f"Live page: {url}"))
    detail = _clean(c.get("detail"), 300)
    if detail:
        after.append(callout(detail, "ok", "What changed"))
    return _note(c, subject, line.format(name=_name(c)), cta, note=note, after=after)


_NEEDS = {
    "captcha": ("A quick check needs you", "A CAPTCHA paused work on {name}.", "Complete the check"),
    "approval": ("Your approval is waiting", "Work on {name} is waiting for your approval.", "Review and approve"),
    "blocked": ("Charlie needs your input", "I need your input to continue work on {name}.", "Help Charlie continue"),
}


def _needs_you(c):
    subject, line, cta = _NEEDS.get(str(c.get("reason")), _NEEDS["blocked"])
    detail = _clean(c.get("detail"), 240)
    return _note(c, subject, line.format(name=_name(c)), cta, note=detail)


def _reminder(c):
    return _note(c, "Your reminder", _clean(c.get("detail"), 240) or "You asked me to remind you.", "Open Supymem",
                 note=f"You set this reminder for {_name(c)}.")


def _digest(c):
    return _note(c, "Your day in Supymem", "Here's what moved forward today.", "See today's progress",
                 after=[item_list(_items(c))])


def _weekly(c):
    after = [item_list(_items(c))]
    step = _clean(c.get("next_step"))
    if step:
        after.append(callout(step, "info", "Your next step"))
    if c.get("signup_count") is not None:
        after.append(paragraph(f"{_count(c['signup_count'])} waitlist signups this week, unverified interest."))
    return _note(c, "Your week in Supymem", "Here's your progress this week.", "See your next step", after=after)


def _weekly_learning(c):
    from core.copy import founder_copy

    after = []
    for key, title, empty in (("changes", "What changed", "No change was recorded for your saved ideas."),
                              ("ended_tests", "Tests ended without working", "No tests ended with a negative result.")):
        values = c.get(key) or [empty]
        after.append(callout("\n".join(founder_copy(_clean(value, 400)) for value in values), "info", title))
    after.append(callout(founder_copy(_clean(c.get("surprise"), 400)), "info", "Your one surprise"))
    after.append(callout(founder_copy(_clean(c.get("next_step"), 600)), "info", "Your recommended decision"))
    note = _note(c, "Your week of learning", "Here's what your recorded tests showed this week.",
                 "Review your next decision", after=after)
    path = _path(c)
    if path == "/connections" or re.fullmatch(r"/v/[A-Za-z0-9_-]+/market-map", path):
        note["url"] = app_url() + path
    return note


def _suggestions(c):
    return _note(c, "Suggestions are ready to review", "I found a few possible next moves.", "Review suggestions",
                 note="Nothing happens until you approve.", after=[item_list(_items(c))])


def _nudge(days: int):
    def build(c):
        subject = {2: "Your next step is waiting", 5: "Pick up where you paused", 14: "Your work is here", 30: "Ready when you are"}[days]
        line = _clean(c.get("next_step") or c.get("detail")) or f"Your work on {_name(c)} is ready when you are."
        note = "This is my last check-in. Your work stays here whenever you come back." if days == 30 else "Open it whenever you're ready."
        return _note(c, subject, line, _clean(c.get("cta_label"), 60) or "See your next step", note=note)
    return build


def _beta_invite(c):
    code = _clean(c.get("code"), 64)
    d = _note(c, "You're invited to Supymem", f"{_clean(c.get('inviter'), 60) or 'The Supymem team'} invited you to try Supymem.", "Get Supymem for Mac",
              note="Charlie helps you research an idea, plan the next step and build a page.",
              after=[callout(code, "info", "Your beta code"), paragraph("Download the Mac app, then enter this code when you sign up.")], path="/download")
    d["url"] = _beta_download_url(code)
    d["why"] = "You're getting this one-time invitation because someone at Supymem entered your address. If you're not interested, ignore it."
    return d


def _god_mode_requested(c):
    return _note(c, "Your God mode request is in", f"I received your request for {_name(c)}.", "View your request",
                 note="Nothing changes until it's approved. We'll reply when it's reviewed.")


def _god_mode_admin(c):
    return _note(c, "God mode request received", f"A founder requested God mode for {_name(c)}.", "Review the request", path="/admin/god-mode")


def _error_alert(c):
    kind = {"new": "New app error", "spike": "App error spike", "regression": "An app error returned"}.get(str(c.get("kind")), "App error reported")
    recent, total = _count(c.get("window_count")), _count(c.get("count"))
    return _note(c, kind, f"{recent} occurrence{'' if recent == 1 else 's'} in the last 10 minutes.", "Review app errors",
                 note=f"{total} total occurrence{'' if total == 1 else 's'}. Review the errors page for details.", path="/admin/errors")


def _finish_setup(c):
    return _note(c, "Tell Charlie your idea", "Tell Charlie your idea, it takes a minute.", "Start your first venture", path="/new")


def _get_mac_app(c):
    download = _admission_download_url(c)
    if not download:
        raise ValueError("email_admission_code_required")
    d = _note(c, "Bring Charlie to your Mac", "Get Supymem for Mac to continue from your desktop.", "Get Supymem for Mac")
    d["url"] = download
    return d


def _idea_stalled(c):
    return _note(c, "One question before we start", f"One answer will help me start researching {_name(c)}.", "Answer Charlie's question",
                 note=_clean(c.get("detail")) or "Open your idea to see the question.")


def _research_started(c):
    return _note(c, "Research is running", f"I'm researching {_name(c)} in the background.", "View research progress",
                 note="You can leave it with me. I'll share the report when it's ready.")


def _beta_feedback(c):
    return _note(c, "How did Supymem feel?", "What was the one thing that made your first venture harder than it should be?", "Open your venture",
                 note="Reply to this email to share your answer.")


def _credits_low(c):
    return _credits_exhausted(c)


def _credits_exhausted(c):
    from app.credits.errors import LIMIT_MESSAGE

    return _note(c, "You have reached your usage limit", LIMIT_MESSAGE, "Buy more",
                 path="/account#account-buy")


def _problem_received(c):
    return _note(c, "We received your problem report", "Thanks for reporting the problem, we're on it.", "Back to Supymem",
                 note="Reply here if there's anything else we should know.")


def _problem_resolved(c):
    return _note(c, "Your report was marked resolved", "Your problem report was marked resolved.", "Back to Supymem",
                 note="Reply if it still needs attention.")


def _progress_summary(c):
    return _note(c, "Your venture moved forward", f"Here's the latest progress on {_name(c)}.", _clean(c.get("cta_label"), 60) or "See your next step",
                 after=[item_list(_items(c))])


def _outreach_drafts_ready(c):
    count = _count(c.get("count"))
    journey = c.get("_gtm_journey") is True
    return _note(c, "Your outreach drafts are ready", f"{count} outreach draft{'s are' if count != 1 else ' is'} ready to review.", "Review drafts" if journey else "Open venture chat",
                 note="Nothing is sent until you approve.",
                 after=[] if journey else [paragraph("In Previous chats, choose Shared saved history. Ask Charlie to show your outreach drafts.")])


def _outreach_sent(c):
    count = _count(c.get("count"))
    return _note(c, "Your approved outreach was sent", f"Your approved outreach sent {count} message{'s' if count != 1 else ''} through Gmail.", "Review outreach" if c.get("_gtm_journey") is True else "Open your venture",
                 note="Open the Sent folder in your connected Gmail to review the messages.")


def _replies_received(c):
    count = _count(c.get("count"))
    return _note(c, "Your outreach has new replies", f"Your outreach received {count} new repl{'ies' if count != 1 else 'y'}.", "Open venture chat",
                 after=[paragraph("In Previous chats, choose Shared saved history for saved updates. Open your connected Gmail to read and answer replies.")])


def _email_health_alert(c):
    reason = {"bounce_spike": "Bounce threshold reached.", "complaint_spike": "Complaint threshold reached.",
              "delivery_failures": "Email delivery failures need review.", "config_errors": "Email configuration needs review."}.get(str(c.get("reason")), "Email health needs review.")
    return _note(c, "Email health needs attention", reason, "Open admin settings",
                 note="Inspect GET /admin/email/status before resuming lifecycle messages.", path="/admin/settings")


def _signup_summary(c):
    count = _count(c.get("count"))
    return _note(c, "Yesterday's beta signups", f"{count} founder{'s' if count != 1 else ''} joined the beta yesterday.", "Review beta users", path="/admin/users")


def _credit_request_admin(c):
    return _note(c, "A founder requested credits", "A new beta credit request is waiting.", "Review credit requests", path="/admin/credits")


def _task_finished(c):
    return _note(c, "Your task is ready to review", f"A task for {_name(c)} finished.", "Review the task",
                 note="Review the result before deciding what to do next.")


def _task_failed(c):
    return _note(c, "A task needs your attention", f"A task for {_name(c)} stopped and needs your review.", "Review the task",
                 note=_clean(c.get("detail")))


def _action_failed(c):
    return _note(c, "A task needs your review", f"A task for {_name(c)} could not finish.", "Review and retry",
                 note=_clean(c.get("detail")))


def _app_release(ctx):
    from app.release_notices.service import build_template

    return build_template(ctx)


def _venture_export(ctx):
    return _note(ctx, "Your venture export is prepared", f"The export for {_clean(ctx.get('venture_name'), 80)} is prepared.",
                 "Open venture settings", note="This confirms preparation of the export, not completion of a download.")


def _venture_archive(ctx):
    return _note(ctx, "Your venture is archived", f"{_clean(ctx.get('venture_name'), 80)} is archived.",
                 "Open recently deleted ventures",
                 note=f"You can restore it from Recently deleted until {_clean(ctx.get('restore_until'), 80)} (7 days after archiving). Data erasure is not confirmed by this receipt.",
                 path="/")


# template -> (category, builder). New lifecycle templates are release-gated in service.
TEMPLATES = {
    "welcome": (TRANSACTIONAL, _welcome),
    "milestone": ("product_updates", _milestone),
    "needs_you": (TRANSACTIONAL, _needs_you),
    "digest_daily": ("digests", _digest),
    "weekly_insights": ("digests", _weekly),
    "weekly_learning": ("digests", _weekly_learning),
    "suggestions_waiting": ("suggestions", _suggestions),
    "nudge_d2": ("engagement_reminders", _nudge(2)),
    "nudge_d5": ("engagement_reminders", _nudge(5)),
    "nudge_d14": ("engagement_reminders", _nudge(14)),
    "beta_invite": (TRANSACTIONAL, _beta_invite),
    "god_mode_requested": (TRANSACTIONAL, _god_mode_requested),
    "god_mode_admin": (TRANSACTIONAL, _god_mode_admin),
    "error_alert": (TRANSACTIONAL, _error_alert),
    "finish_setup": ("engagement_reminders", _finish_setup),
    "get_mac_app": ("product_updates", _get_mac_app),
    "idea_stalled": ("engagement_reminders", _idea_stalled),
    "research_started": ("product_updates", _research_started),
    "beta_feedback": ("engagement_reminders", _beta_feedback),
    "credits_low": (TRANSACTIONAL, _credits_low),
    "credits_exhausted": (TRANSACTIONAL, _credits_exhausted),
    "problem_received": (TRANSACTIONAL, _problem_received),
    "problem_resolved": (TRANSACTIONAL, _problem_resolved),
    "nudge_d30": ("engagement_reminders", _nudge(30)),
    "progress_summary": ("product_updates", _progress_summary),
    "outreach_drafts_ready": ("product_updates", _outreach_drafts_ready),
    "outreach_sent": (TRANSACTIONAL, _outreach_sent),
    "replies_received": ("product_updates", _replies_received),
    "email_health_alert": (TRANSACTIONAL, _email_health_alert),
    "signup_summary": (TRANSACTIONAL, _signup_summary),
    "credit_request_admin": (TRANSACTIONAL, _credit_request_admin),
    "task_finished": ("product_updates", _task_finished),
    "task_failed": ("product_updates", _task_failed),
    "action_failed": (TRANSACTIONAL, _action_failed),
    "app_release": ("product_updates", _app_release),
    "venture_export_prepared": (TRANSACTIONAL, _venture_export),
    "venture_archived": (TRANSACTIONAL, _venture_archive),
    "reminder": (TRANSACTIONAL, _reminder),
}


def category_of(template: str) -> str:
    return TEMPLATES[template][0]


def render(template: str, ctx: dict, *, unsubscribe_url: str = "", prefs_url: str = "") -> tuple[str, str, str]:
    """Returns (subject, html, text). Only one primary CTA, then optional details."""
    d = TEMPLATES[template][1](ctx)
    blocks = [*d["blocks"], button(d["cta"], d.get("url") or _chat_url(ctx, d["path"])), *d.get("after", [])]
    html, text = _layout(d["head"], blocks, pre=d["pre"], address=os.environ.get("EMAIL_PHYSICAL_ADDRESS", DEFAULT_ADDRESS),
                         unsubscribe_url=unsubscribe_url, prefs_url=prefs_url, why=d.get("why", ""))
    return d["subject"], html, text

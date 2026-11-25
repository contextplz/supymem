"""Every needs_you request must say what is blocked and give numbered steps; these fill the gaps by reason_code."""
from __future__ import annotations

import re

GENERIC_HOW = "Tap Continue once it is done and I carry on."
GENERIC_WHAT = "Something only you can do."
# reason_code -> (what is blocked, numbered steps WITHOUT the final "tap Continue")
TEMPLATES: dict[str, tuple[str, list[str]]] = {
    "login": ("A sign-in only you can do is holding the work up.",
              ["Open the app or site named in this request and sign in yourself (Supymem never types your password)",
               "Leave that window signed in"]),
    "two_factor": ("A one-time code from you is holding the work up.",
                   ["Open your authenticator app or text messages and find the code",
                    "Type it into the window Supymem opened (never into chat)"]),
    "captcha": ("A CAPTCHA is holding the work up and only a person can solve it.",
                ["Open the window Supymem opened on your Mac", "Solve the CAPTCHA yourself"]),
    "verification": ("A verification step is holding the work up.",
                     ["Open the email, text or app that sent you the verification",
                      "Finish it in the window Supymem opened"]),
    "payment": ("A payment only you can make is holding the work up.",
                ["Open the billing page named in this request", "Review the amount and pay with your own card"]),
    "os_permission": ("macOS permissions for Supymem are off, so the Mac task cannot run.",
                      ["Open System Settings > Privacy & Security",
                       "Turn Screen Recording and Accessibility on for Supymem",
                       "Quit and reopen Supymem if macOS asks"]),
    "consent_send": ("I will not send anything until you approve it.",
                     ["Open the draft here in chat", "Read it and fix anything you do not like",
                      "Approve it if it is right"]),
    "consent_delete": ("I will not delete anything until you approve it.",
                       ["Open the item named in this request", "Check it is safe to remove", "Approve the deletion"]),
    "consent_spend": ("I will not spend credits or money until you approve it.",
                      ["Open the card in chat and read the cost", "Approve it, or say no"]),
    "credit_cap": ("There are insufficient credits, so paid work is paused.",
                   ["Open Credits", "Add credits to continue"]),
    "gmail_connect": ("Gmail is not connected yet, so I cannot read or send mail for you.",
                      ["Open Connections in the sidebar and choose your company",
                       "On the Gmail card click Connect",
                       "Click Open Gmail authorization, pick your Google account and click Allow",
                       "Come back to Connections and click Refresh all"]),
    "real_world_action": ("A real-world step only you can do is holding the work up.",
                          ["Do the step named in this request"]),
    "ambiguous_high_stakes": ("There is a big choice I should not make for you.",
                              ["Read the options in chat", "Reply with the one you want"]),
    "exhausted": ("I tried every way I know and still cannot move without you.",
                  ["Open Right now and read what failed", "Fix it, or tell me another way in chat"]),
}
DEFAULT = ("Something only you can do is holding the work up.",
           ["Open the card in chat and read what it asks", "Do that one thing"])
_TAIL = re.compile(r"[\s,;.]*(?:then|and)?\s*tap\s+continue\b.*$", re.I | re.S)
_NUMBERED = re.compile(r"^\s*1[.)]\s", re.M)


def steps_text(steps: list[str]) -> str:
    return "\n".join(f"{i}. {s.strip().rstrip('.')}." for i, s in enumerate(steps, 1))


def explain(reason_code: str, what: str, how: str) -> tuple[str, str]:
    """Never empty: (what, numbered steps). Caller text wins; the template fills blanks and generic placeholders."""
    t_what, t_steps = TEMPLATES.get(reason_code, DEFAULT)
    what = (what or "").strip()
    if not what or what == GENERIC_WHAT:
        what = t_what
    how = _TAIL.sub("", (how or "").strip()).strip()
    if not how:
        return what, steps_text(t_steps)
    if _NUMBERED.search(how):
        return what, how
    return what, steps_text([how])


def blocker_message(rows: list[dict], *, flat: bool = False) -> str:
    """'Hey, I have a blocker: <what>. Here's exactly what to do: <steps>. Then tap Continue.' for one or many rows."""
    sep = " " if flat else "\n"

    def steps(r: dict) -> str:
        return " ".join(r["how_to_resume"].split("\n")) if flat else "\n" + r["how_to_resume"]

    if len(rows) == 1:
        r = rows[0]
        tail = "" if r["reason_code"] == "mac_native_approval_required" else f"{sep}Then tap Continue."
        what = r["what_we_need"].strip()
        if what and what[-1] not in ".!?":
            what += "."
        return (f"Hey, I have a blocker: {what} Here's exactly what to do: {steps(r)}"
                f"{tail}")
    parts = [f"{i}) {r['title']}. {steps(r)}" for i, r in enumerate(rows, 1)]
    tail = "" if any(r["reason_code"] == "mac_native_approval_required" for r in rows) else f"{sep}Then tap Continue on each."
    return f"Hey, I have {len(rows)} blockers.{sep}" + (sep * (1 if flat else 2)).join(parts) + tail

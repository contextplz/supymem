"""gstack coding charter — applied to every Work Order prompt.

Upstream methodology: garrytan/gstack (MIT). Kept inside this package so
coding_agents never imports company_os. Dispatcher wraps prompts here.
"""

from __future__ import annotations

SENTINEL = "GSTACK METHODOLOGY"

_CHARTER = """GSTACK METHODOLOGY (garrytan/gstack, MIT) — follow this before writing code.

Iron Law: NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST.
1. Collect symptoms (error, stack, repro). 2. Read the code path. 3. Check recent git history.
4. Reproduce. 5. Write a testable root-cause hypothesis. 6. Confirm it with evidence.
7. Then make the smallest change that removes the cause. 8. Add a regression test that
FAILS without the fix and PASSES with it. 9. Re-run tests. Never say "this should fix it."

3-strike rule: if three hypotheses fail, STOP and escalate as an architectural issue.
Do not shotgun-fix. If the fix touches >5 files, say so before proceeding.

Pre-landing review (do this before you claim done):
- SQL / persistence safety, LLM trust boundaries, conditional side effects
- nil / empty / upstream-error paths on every new data flow
- named errors (exception, trigger, user-visible result) — no catch-alls
- tests for the new path; observability for how we'd know it broke in production

QA loop: test → atomic fix → re-verify → regression test → ship-readiness.
CSO: any security claim needs a concrete exploit scenario; skip theater.

UI work — refuse AI slop: purple/violet gradient defaults, 3-column icon grids,
centered-everything, bubbly radius on everything, gradient CTAs, Inter/system-ui-only type,
"Built for X" / "AI-powered" / "seamless" copy.

When approaches compete: pick the simpler explicit design. Do not silently expand scope.
"""


def wrap_prompt(prompt: str, *, kind: str = "code") -> str:
    text = (prompt or "").strip()
    if not text or SENTINEL in text:
        return prompt
    extra = ""
    if kind in {"debug", "fix", "investigate"}:
        extra = "\nYou are in INVESTIGATE mode. Do not change code until the hypothesis is written.\n"
    return _CHARTER + extra + "\nTASK:\n" + text

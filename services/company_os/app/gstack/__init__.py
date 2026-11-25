"""gstack methodologies adapted for Company OS.

Upstream: https://github.com/garrytan/gstack (MIT, Garry Tan).
We do not run gstack's Claude Code slash commands or Bun browse daemon.
We take the algorithms — office hours, CEO review, autoplan, investigate,
design anti-slop, CSO, QA — and execute them inside Company OS organs.
"""

from app.gstack.methodology import (
    AUTOPLAN_PRINCIPLES,
    CEO_MODES,
    CEO_PRIME_DIRECTIVES,
    CSO_GATES,
    DESIGN_SLOP,
    FORCING_QUESTIONS,
    INVESTIGATE_IRON_LAW,
    QA_LOOP,
    office_hours_options,
    office_hours_prompt,
    office_hours_why,
    specialist_for_role,
)
from app.gstack.prompts import (
    ceo_review_brief,
    design_copy_rules,
    wrap_coding_prompt,
)

__all__ = [
    "AUTOPLAN_PRINCIPLES",
    "CEO_MODES",
    "CEO_PRIME_DIRECTIVES",
    "CSO_GATES",
    "DESIGN_SLOP",
    "FORCING_QUESTIONS",
    "INVESTIGATE_IRON_LAW",
    "QA_LOOP",
    "ceo_review_brief",
    "design_copy_rules",
    "office_hours_options",
    "office_hours_prompt",
    "office_hours_why",
    "specialist_for_role",
    "wrap_coding_prompt",
]

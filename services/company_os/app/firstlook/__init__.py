"""First look: the instant demo landing page (doc 45).

Copy still comes from one model call (or a plain fallback). Layout is planned
from the idea, then coded as unique self-contained HTML — not one of seven
Jinja skins. OpenCode writes the page when the CLI is present; otherwise the
composer does. Fabrication is still rejected by :mod:`app.firstlook.lint`.
"""

from __future__ import annotations

from app.firstlook.service import (
    apply_founder_copy,
    current_site,
    generate_first_look,
    record_interest,
    refine_from_charter,
    try_another_look,
)

__all__ = [
    "apply_founder_copy",
    "current_site",
    "generate_first_look",
    "record_interest",
    "refine_from_charter",
    "try_another_look",
]

"""Pick a first-look Jinja template by archetype (45 §3.2)."""

from __future__ import annotations

from pathlib import Path

# Canonical template names under config/firstlook/templates/.
TEMPLATES = frozenset(
    {"marketplace", "tool", "consumer", "local", "content", "physical", "generic"}
)

# Map classifier / free-form archetype labels onto the seven templates.
_ALIASES: dict[str, str] = {
    "marketplace": "marketplace",
    "two-sided": "marketplace",
    "two_sided": "marketplace",
    "two sided": "marketplace",
    "tool": "tool",
    "saas": "tool",
    "b2b": "tool",
    "b2b saas": "tool",
    "crud_web": "tool",
    "crud web": "tool",
    "integration_bot": "tool",
    "integration bot": "tool",
    "consumer": "consumer",
    "consumer app": "consumer",
    "app": "consumer",
    "local": "local",
    "local service": "local",
    "service": "local",
    "content": "content",
    "media": "content",
    "content / media": "content",
    "physical": "physical",
    "hardware": "physical",
    "hardware / physical": "physical",
    "generic": "generic",
    "product": "generic",
    "ecommerce": "consumer",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def templates_dir() -> Path:
    return _repo_root() / "config" / "firstlook" / "templates"


def normalize_archetype(archetype: str | None) -> str:
    raw = (archetype or "generic").strip().lower().replace("-", "_")
    spaced = raw.replace("_", " ")
    if raw in _ALIASES:
        return _ALIASES[raw]
    if spaced in _ALIASES:
        return _ALIASES[spaced]
    if "marketplace" in raw or "two" in raw and "side" in raw:
        return "marketplace"
    if any(k in raw for k in ("saas", "b2b", "tool", "crud", "bot")):
        return "tool"
    if any(k in raw for k in ("consumer", "app")):
        return "consumer"
    if "local" in raw or "service" in raw:
        return "local"
    if any(k in raw for k in ("content", "media", "newsletter", "podcast")):
        return "content"
    if any(k in raw for k in ("physical", "hardware", "device")):
        return "physical"
    return "generic"


def pick_template(archetype: str | None) -> str:
    name = normalize_archetype(archetype)
    return name if name in TEMPLATES else "generic"


def template_path(archetype: str | None) -> Path:
    name = pick_template(archetype)
    path = templates_dir() / f"{name}.html.j2"
    if not path.exists():
        return templates_dir() / "generic.html.j2"
    return path


__all__ = [
    "TEMPLATES",
    "normalize_archetype",
    "pick_template",
    "template_path",
    "templates_dir",
]

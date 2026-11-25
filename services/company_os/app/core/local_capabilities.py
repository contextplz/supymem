"""Owner-controlled local features, with readiness from your saved settings."""

from urllib.parse import urlsplit
import re

from app.core.local_mode import is_local
from app.core.local_secrets import get_secret, read_settings


REGISTRY = {
    "research": ("Research", True),
    "find_customers": ("Find customers", True),
    "send_email": ("Send email", False),
    "read_replies": ("Read replies", False),
    "landing_pages": ("Draft landing pages", True),
    "publish_pages": ("Publish pages", False),
    "page_measurement": ("Page measurement", True),
    "background_work": ("Background work", False),
    "run_for_me_experiments": ("Run your experiments", False),
    "routines": ("Routines", False),
    "connected_apps": ("Connected apps", False),
    "content": ("Draft content", True),
    "build_with_my_tool": ("Build with your coding tool", True),
}

UNAVAILABLE = {
    "computer_use": {"title": "Computer use", "next_step": "Computer use is not in this version yet; ask me to write a brief for your connected coding tool instead.",
        "surfaces": {"paths": ["/computer-use", "/computer", "/mac-tasks", "/setup/permissions"], "actions": ["mac.task.prepare", "browser.task.prepare"], "settings": ["mac"], "guide": ["mac"]}},
    "voice": {"title": "Voice", "next_step": "Voice is not in this version yet; type your request in chat instead.",
        "surfaces": {"paths": ["/voice"], "settings": ["voice"], "guide": ["voice"]}},
    "social_posting": {"title": "Social posting", "next_step": "Social posting is not in this version yet; ask me to draft your post, then publish it yourself instead.",
        "surfaces": {"paths": ["/social", "/marketing"], "actions": ["social_post", "social_connect", "social_status", "social_guide"], "settings": ["social"],
                     "connections": ["linkedin", "reddit", "x", "twitter", "instagram", "facebook", "tiktok", "zernio", "outstand"]}},
    "ads": {"title": "Ads", "next_step": "Ads are not in this version yet; ask me to plan an email or landing page experiment instead.",
        "surfaces": {"paths": ["/ads"], "settings": ["ads", "google_ads", "meta_ads"], "connections": ["google_ads", "meta_ads"]}},
    "lead_library": {"title": "Purchased contacts", "next_step": "Purchased contacts are not in this version yet; import contacts you have permission to use or ask me to find customers instead.",
        "surfaces": {"paths": ["/lead-library", "/gtm/library"], "actions": ["gtm.library_claim", "gtm.library_more", "gtm.library_notes"]}},
    "admin": {"title": "Administration", "next_step": "Administration is not in this version yet; open Settings > Keys and connections to manage your app instead.",
        "surfaces": {"paths": ["/admin", "/operator", "/ops"], "settings": ["admin"]}},
    "billing": {"title": "Credit purchases", "next_step": "Credit purchases are not in this version yet; open Settings > Keys and connections to use your own service keys instead.",
        "surfaces": {"paths": ["/billing", "/credits", "/stripe", "/plans", "/pricing", "/subscription", "/subscriptions", "/checkout", "/buy-more", "/top-up", "/topup", "/referral", "/referrals"],
                     "actions": ["referrals.show"], "settings": ["billing", "credits"]}},
    "hosted_connected_apps": {"title": "Other app connections", "next_step": "These app connections are not in this version yet; open Connections to choose a supported app or your coding tool instead.",
        "surfaces": {"paths": ["/community"], "connections": ["gmail", "unipile", "hunter", "discord", "stripe"]}},
}


def unavailable_message(surface: str, value: str) -> str | None:
    if not is_local():
        return None
    if surface == "connections":
        from app.connections.local_config import auth_config_envs

        if auth_config_envs(value):
            return None
    if surface == "paths":
        value = re.sub(r"^/v/[^/]+", "", value.split("?", 1)[0].split("#", 1)[0])
    for name, item in UNAVAILABLE.items():
        matches = item["surfaces"].get(surface, ())
        if any(value == match or surface == "paths" and value.startswith(match + "/") for match in matches):
            feature = capability(name)
            if feature["state"] == "not_in_this_version":
                return feature["next_step"]
    return None


def surface_visible(surface: str, value: str) -> bool:
    return unavailable_message(surface, value) is None


def hidden_request_message(text: str, action: str) -> str | None:
    reason = unavailable_message("actions", {"mac_prepare": "mac.task.prepare"}.get(action, action))
    if reason or not is_local() or action in {"build.tool.start", "start_product_build", "pagebuilder.build"}:
        return reason
    # shortcut: match explicit English requests here; add translated patterns when deterministic routing in other languages is required.
    patterns = {
        "voice": r"(?:start|enable|turn on|use)\s+(?:a\s+)?voice(?:\s+chat)?\b|(?:talk|speak)\s+to\s+me\b",
        "social_posting": r"(?:post|publish|share|connect)\b.{0,80}\b(?:linkedin|twitter|instagram|facebook|tiktok|social|reddit)\b",
        "ads": r"(?:start|run|launch|create|buy)\s+(?:(?:an?|my|your|paid|google|meta|facebook)\s+)*(?:ads?|advertising|ad campaign)\b",
        "billing": r"(?:buy|purchase|top up)\s+(?:more\s+)?credits\b",
        "admin": r"(?:open|show)\s+(?:the\s+)?admin(?:istration)?\b",
        "computer_use": r"(?:use|control|operate)\s+(?:(?:my|the)\s+)?(?:computer|desktop|mac)\b",
        "lead_library": r"(?:buy|purchase)\s+(?:(?:some|more)\s+)?(?:leads|contacts)\b",
    }
    request = re.sub(r"^\s*(?:(?:please|can you|could you|would you)\s+)+", "", text, flags=re.I).strip()
    for name, pattern in patterns.items():
        if name == "computer_use" and action == "open_screen":
            continue
        if re.match(pattern, request, re.I):
            feature = capability(name)
            if feature["state"] == "not_in_this_version":
                return feature["next_step"]
    return None


def _field(section, name):
    from app.api.local_settings import FIELD_INFO

    return section, FIELD_INFO[section][name][0]


def _present(value):
    return isinstance(value, str) and bool(value.strip())


def _model_missing(*, search=False):
    from app.core.providers_local import LocalProviderError, _loopback, _sample_selected, configured_providers

    if not search and _sample_selected():
        return []
    try:
        settings = configured_providers()
    except LocalProviderError:
        settings = {"provider": None, "providers": {}}
    provider = settings["provider"]
    selected = settings["providers"].get(provider, {})
    if selected.get("configured") and (not search or provider in {"openai", "azure_openai"}
                                       and selected.get("api_key_set")):
        return []
    if search and provider not in {"openai", "azure_openai"}:
        return [("providers", _field("providers", "provider")[1] + " (OpenAI or Azure OpenAI)")]
    missing = [] if selected else [_field("providers", "provider")]
    if not selected.get("base_url"):
        missing.append(_field("providers", "base_url"))
    if not selected.get("models", {}).get("chat"):
        missing.append(_field("providers", "model"))
    keyless = provider == "openai_compatible" and _loopback(urlsplit(selected.get("base_url") or "").hostname)
    if not selected.get("api_key_set") and not keyless:
        missing.append(_field("providers", "api_key"))
    return missing


def _search_missing():
    values = read_settings("search")["values"]
    provider = values.get("provider", "builtin")
    if not isinstance(provider, str):
        return [_field("search", "provider")]
    if provider == "builtin":
        return []
    if provider in {"tavily", "exa"}:
        return [] if _present(get_secret("search", "api_key")) else [_field("search", "api_key")]
    if provider == "openai":
        return _model_missing(search=True)
    if provider == "firecrawl":
        return [] if _present(values.get("firecrawl_url")) else [_field("search", "firecrawl_url")]
    return [_field("search", "provider")]


def _fields_missing(section, fields, secrets=()):
    values = read_settings(section)["values"]
    return [_field(section, field) for field in fields
            if not _present(get_secret(section, field) if field in secrets else values.get(field))]


SMTP_SEND_FIELDS = ("user", "password", "host", "sender_name", "postal_address", "port")


def missing_mail_fields(values: dict, password: str | None, *, incoming: bool = False) -> list[str]:
    fields = ("user", "password", "imap_host", "imap_port") if incoming else SMTP_SEND_FIELDS
    port, default = ("imap_port", 993) if incoming else ("port", 587)
    value = values.get(port, default)
    missing = [field for field in fields if field != port and not _present(password if field == "password" else values.get(field))]
    if type(value) is not int or not 1 <= value <= 65535:
        missing.append(port)
    return missing


def _missing(name):
    if name == "build_with_my_tool":
        from app.coding_agents_consent import consents
        from app.workspaces.resolve import local_person
        from desktop_runtime.agent_status import statuses

        allowed = consents(local_person().id)
        if any(allowed.get(tool.get("name")) is True
               and tool.get("name") in {"claude", "codex", "cursor", "opencode"}
               and all(tool.get(field) is True for field in ("installed", "signed_in", "subscription_login", "available"))
               for tool in statuses()):
            return []
        return [("coding_tools", "a connected coding tool with a verified subscription login")]
    if name == "research":
        return _search_missing()
    if name == "find_customers":
        return _search_missing() + _model_missing()
    if name == "landing_pages":
        return _missing("build_with_my_tool")
    if name in {"content", "run_for_me_experiments"}:
        return _model_missing()
    if name in {"send_email", "read_replies"}:
        values = read_settings("smtp")["values"]
        missing = missing_mail_fields(values, get_secret("smtp", "password"), incoming=name == "read_replies")
        return [_field("smtp", field) for field in missing]
    if name == "publish_pages":
        values = read_settings("publishing")["values"]
        provider = values.get("provider")
        if not isinstance(provider, str):
            return [_field("publishing", "provider")]
        if provider in {"vercel", "netlify"}:
            return _fields_missing("publishing", ["project", "token"], ("token",))
        if provider == "s3":
            return _fields_missing("publishing", ["bucket", "region", "prefix", "site_url", "access_key_id", "secret_access_key"],
                ("access_key_id", "secret_access_key"))
        return [_field("publishing", "provider")]
    if name == "connected_apps":
        missing = _fields_missing("composio", ["api_key"], ("api_key",))
        configs = read_settings("composio")["values"].get("auth_configs")
        if not isinstance(configs, dict) or not any(_present(value) for value in configs.values()):
            missing.append(_field("composio", "auth_configs"))
        return missing
    return []


def capability(name: str) -> dict:
    if name in UNAVAILABLE:
        item = UNAVAILABLE[name]
        return {"name": name, **item, "state": "not_in_this_version" if is_local() else "ready", "missing": []}
    from app.api.local_settings import SECTION_INFO
    from app.core.local_worker import background_jobs

    title, default = REGISTRY[name]
    if not is_local():
        return {"name": name, "title": title, "state": "not_in_this_version", "missing": [],
                "next_step": "Open your local app, then go to Settings > Capabilities."}
    on = read_settings("capabilities")["values"].get(name, default) is True
    missing = _missing(name)
    state = "off" if not on else "needs_setup" if missing else "ready"
    steps = [] if on else [f"In Settings > Capabilities, switch on {title}."]
    if name == "find_customers" and on:
        research = capability("research")
        if research["state"] == "off":
            state = "needs_setup"
            steps.append(research["next_step"])
    for section in dict.fromkeys(section for section, label in missing):
        if section == "coding_tools":
            if name != "landing_pages":
                steps.append("In Settings > Keys and connections > Coding tools, connect Claude Code, Codex, Cursor or OpenCode, "
                             "sign in with your own subscription and choose Allow Charlie, then check the connection again.")
            continue
        fields = ", ".join(label for field_section, label in missing if field_section == section)
        steps.append(f"In Settings > Keys and connections > {SECTION_INFO[section][0]}, save your {fields}.")
    if name == "landing_pages" and on:
        build = capability("build_with_my_tool")
        if build["state"] != "ready":
            state = "needs_setup"
            steps.append(build["next_step"])
            missing.append(("coding_tools", "Your connected coding tool"))
    jobs = background_jobs(name)
    next_step = steps[0] if steps else f"You can use {title}. Choose the work you want in your workspace."
    if jobs and not steps:
        next_step += (f" Your saved work limits stay in place. Set your monthly spending limit in "
                      f"Settings > Keys and connections > {SECTION_INFO['spend'][0]}.")
    return {"name": name, "title": title, "state": state, "missing": [label for section, label in missing],
            "next_step": next_step, "background_jobs": jobs}


def enabled(name: str) -> bool:
    return capability(name)["state"] == "ready"


def blocked_message(name: str) -> str:
    return capability(name)["next_step"]


def list_capabilities() -> list[dict]:
    return [capability(name) for name in (*REGISTRY, *UNAVAILABLE)]

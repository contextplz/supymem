"""Explicit product build requests, shared by chat and device-order admission."""
import re

from app.core.local_mode import is_local

_LEAD = re.compile(r"^\s*(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|would like)\s+(?:you\s+)?to\s+|i['’]d\s+like\s+(?:you\s+)?to\s+|help\s+me\s+|let['’]?s\s+)?(?:please\s+)?"
    r"(?:(?:build|implement|develop|create|make|write(?=\s+(?:(?:the|my|our)\s+)?code\b))\b|use\s+(?:codex|claude(?: code)?|cursor|opencode)\s+to\s+(?:build|implement|develop|create|make|write(?=\s+(?:(?:the|my|our)\s+)?code\b))\b)", re.I)
_PRODUCT = re.compile(r"\b(?:app|application|product|full[ -]stack|this|that|idea|plan)\b", re.I)
_PROVIDER = re.compile(r"\b(?:with|using|via|use)\s+(?:(?:my|our|the)\s+)?(?:codex|claude(?: code)?)\b", re.I)
_BUILD = re.compile(r"\b(?:build|implement|develop)\b", re.I)
_DECLINE = re.compile(r"\b(?:do not|don['’]t|never|not yet)\s+(?:build|implement|develop|create|make|write|use|start|run)\b", re.I)
_AUTOMATIC = re.compile(r"\b(?:autopilot|automatically|scheduled?|every\s+(?:day|hour|week|month)|without\s+(?:my\s+)?approval)\b", re.I)
_EXISTING_REPAIR = re.compile(r"\b(?:repair|fix|edit)\b.{0,100}\b(?:existing|start script|script only|task)\b|\b(?:existing|current|approved)\s+(?:task|start script|script)\b|\bonly\b.{0,80}\b(?:repair|fix|edit)\b", re.I)
_LOCAL_SCOPE = re.compile(r"\b(?:app|application|product|backend|code|full[ -]stack|prototype|mvp|(?:(?:first|working)\s+)+version)\b", re.I)
_LOCAL_DEFER = re.compile(r"\b(?:do not|don['’]t|never|not yet)\s+(?:execute|begin|do|launch)\b|\b(?:if|when|once|until)\s+(?:i|you|we|my|your)\b|\b(?:wait|pause)\s+(?:for|until)\b|\b(?:later|tomorrow|next\s+(?:week|month))\b|\b(?:or|but)\s+should\s+i\b|\bwithout asking\b|\b(?:only|just)\s+(?:explain|describe|tell|show|plan)\b|\b(?:is what|said|quoted|read[ -]only|without\s+(?:starting|running|building))\b", re.I)
_TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "opencode": "OpenCode"}
_TOOL = r"(?:claude(?:\s+code)?|codex|cursor|opencode|(?:my|our|the)\s+(?:connected\s+)?coding\s+tool)"
_LOCAL_VERB = r"build|rebuild|implement|develop|create|make|write|change|fix|edit|update|repair|add|refactor|patch|modify|new\s+version"
_LOCAL_PREFIX = (r"^\s*(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|would like)\s+(?:you\s+)?to\s+|"
    r"i['’]d\s+like\s+(?:you\s+)?to\s+|help\s+me\s+|let['’]?s\s+)?(?:please\s+)?")
_LOCAL_LEAD = re.compile(rf"{_LOCAL_PREFIX}(?:use\s+{_TOOL}\s+to\s+)?(?P<verb>{_LOCAL_VERB})\b", re.I)
_KICKOFF_BUILD_RE = re.compile(
    r"^\s*(?:(?:yes|yeah|yep|yup|ok(?:ay)?|sure|please|now|so|go ahead and|let'?s)[,.!\s]+)*"
    r"(?:kick\s+off\s+(?:the\s+|my\s+|that\s+|this\s+)?(?:(?:landing\s*page|product|app|startup)\s+)?build(?:ing)?|"
    r"start\s+(?:the\s+|my\s+|that\s+)?(?:(?:landing\s*page|product|app)\s+)?build(?:ing)?|"
    r"begin\s+(?:the\s+|my\s+)?build(?:ing)?|build\s+(?:it|that|this)|get\s+(?:the\s+)?build(?:ing)?\s+(?:going|started)|"
    r"(?:start|run)\s+the\s+build)(?:\s+(?:now|please))*\s*[.!]*\s*$",
    re.I,
)
_LOCAL_DECLINE = re.compile(rf"\b(?:do not|don['’]t|never|not yet)\s+(?:{_LOCAL_VERB}|use|start|run)\b", re.I)
_RESULT_RUN_LIMIT = re.compile(
    r"(?:(?<=[,.;!\n])\s*(?:but\s+)?|\bbut\s+)(?:do not|don['’]t|never)\s+run\s+it\s+(?:on|against)\s+"
    r"(?:(?!\b(?:and|or|but|then|do\s+not|don['’]t|never|not\s+yet)\b)[^,;.!?\n])+(?=[.;!?\n]|$)", re.I)
_TOOL_SUFFIX = re.compile(rf"\b(?:with|using|via)\s+(?:(?:my|our|the)\s+)?{_TOOL}\b", re.I)
_PAGE_SCOPE = re.compile(r"\b(?:landing\s*page|web\s*site|website|homepage|page|site)\b", re.I)
_SOURCE_FILE = re.compile(r"\b[\w-]+\.(?:py|[cm]?[jt]sx?|html|css|go|rs|rb|php|java|sh|sql|toml|ya?ml|json)\b", re.I)
_UTILITY_SCOPE = re.compile(r"\b(?:scrapers?|scripts?|utilities|utility|tools?|command[ -]line|cli|"
    r"parsers?|calculators?|functions?|modules?|tests?|bugs?|exports?|endpoints?)\b", re.I)
_CHANGE = re.compile(r"\b(?:change|fix|edit|update|repair|add|refactor|patch|modify)\b", re.I)


def local_build_deferred(source: str) -> bool:
    # shortcut: only a separate, explicit limit on running the result is not a build veto; use scoped parsing for mixed clauses.
    return bool(_LOCAL_DEFER.search(source) or _LOCAL_DECLINE.search(_RESULT_RUN_LIMIT.sub("", source)) or _AUTOMATIC.search(source)
                or re.search(r"\b(?:answer|explain)\s+only\b", source, re.I))


def _local_build_scope(text: str) -> str | None:
    from app.chat import hosting
    from app.chat.llm_router import _command_evidence

    if not is_local() or not isinstance(text, str) or len(text.encode()) > 16000:
        return None
    source = _command_evidence(text)
    lead = _LOCAL_LEAD.match(source)
    kickoff = _KICKOFF_BUILD_RE.fullmatch(text) and not _PAGE_SCOPE.search(text)
    if not (lead or kickoff) or hosting.matches(source) or local_build_deferred(source):
        return None
    if kickoff:
        return "build.tool.start"
    if not lead:
        return None
    if _BUILD.search(lead.group()) and _EXISTING_REPAIR.search(source):
        return None
    verb = lead["verb"]
    if _CHANGE.fullmatch(verb) or verb.lower() == "make":
        from app.chat.site_actions import explicit_page_rewrite

        if explicit_page_rewrite(text, literal=True):
            return "site.rewrite"
    target = _TOOL_SUFFIX.sub("", source[lead.end():]).strip()
    scope = re.split(r"\b(?:for|with|using|via|from|based on|according to)\b", target, maxsplit=1, flags=re.I)[0]
    tool_named = bool(re.search(rf"\b{_TOOL}\b", source, re.I))
    if _SOURCE_FILE.search(scope) and (_CHANGE.fullmatch(verb) or _SOURCE_FILE.match(scope)):
        return "utility"
    if _PAGE_SCOPE.search(scope) and not re.search(r"\b(?:app|application|backend|full[ -]stack)\b", scope, re.I):
        return "pagebuilder.build" if tool_named or _CHANGE.search(verb) or verb.lower() in {"rebuild", "new version"} else None
    utility = _UTILITY_SCOPE.search(scope)
    if utility and utility[0].lower() in {"test", "tests"} and not tool_named:
        from app.chat.intent import discovery_copy_request

        if discovery_copy_request(f"{lead.group()} {scope}"):
            return None
    # shortcut: only admit recognizable code requests; extend these cues when other wording must start work.
    if utility and (re.search(r"\b(?:test\s+plan|A/B\s+test)\b", scope, re.I)
            or utility[0].lower() in {"script", "scripts"} and not tool_named
                and not re.search(r"\b(?:python|javascript|typescript|shell|bash|sql|code|csv|json|parse|convert|validate|export|scrape|summarize)\b", target, re.I)):
        return None
    if utility or _CHANGE.fullmatch(verb) and _LOCAL_SCOPE.search(scope):
        if (re.search(r"\b(?:for|to|from|that|in|on|of|against)\s+\w", target, re.I)
                or _CHANGE.fullmatch(verb) and len(re.findall(r"\b\w+\b", scope.strip())) > 2):
            return "utility"
    if (tool_named or _CHANGE.search(verb) or verb.lower() in {"rebuild", "new version"}) and _LOCAL_SCOPE.search(scope):
        if not re.search(r"\bplan\b", scope, re.I) or verb.lower() == "implement":
            return "build.tool.start"
    return None


def local_build_intent(text: str) -> str | None:
    scope = _local_build_scope(text)
    return "build.tool.start" if scope == "utility" else scope


def is_utility_build_request(text: str) -> bool:
    return _local_build_scope(text) == "utility"


def is_product_build_request(text: str) -> bool:
    if is_local():
        if intent := local_build_intent(text):
            return intent == "build.tool.start"
        from app.chat import hosting
        from app.chat.llm_router import _command_evidence

        if not isinstance(text, str) or len(text.encode()) > 16000:
            return False
        source = _command_evidence(text)
        if hosting.matches(source):
            return False
        lead = _LEAD.match(source)
        if not lead:
            return False
        scope = re.split(r"\b(?:for|with|using|via|from|based on|according to)\b", source[lead.end():], maxsplit=1, flags=re.I)[0]
        implements_plan = bool(re.search(r"\bimplement$", lead.group(), re.I) and re.search(r"\bplan\b", scope, re.I))
        product_scope = (_LOCAL_SCOPE.search(scope) or implements_plan
                         or (_BUILD.search(lead.group()) and re.fullmatch(r"\s*(?:this|that|it|(?:(?:my|our|the)\s+)?idea)(?:\s+now)?\s*[.!?]*\s*", scope, re.I)))
        return bool(product_scope
                    and (not re.search(r"\bplan\b", scope, re.I) or implements_plan)
                    and not local_build_deferred(source)
                    and not _EXISTING_REPAIR.search(source)
                    and not (re.search(r"\blanding\s+page\b", scope, re.I)
                             and not re.search(r"\b(?:app|application|full[ -]stack)\b", scope, re.I)))
    return bool(isinstance(text, str) and len(text.encode()) <= 16000
                and _LEAD.match(text) and _BUILD.search(text) and _PRODUCT.search(text)
                and (is_local() or _PROVIDER.search(text)) and not _DECLINE.search(text)
                and not _AUTOMATIC.search(text) and not _EXISTING_REPAIR.search(text))


def named_coding_tools(text: str) -> set[str]:
    from app.chat.llm_router import _command_evidence

    return {"claude" if name.lower().startswith("claude") else name.lower()
            for name in re.findall(r"\b(codex|claude(?: code)?|cursor|opencode)\b", _command_evidence(text), re.I)}


def local_product_provider(text: str, actor_subject: str | None) -> str | None:
    from fastapi import HTTPException

    from app.chat.llm_router import _command_evidence
    from app.coding_agents_consent import consents, raise_if_tool_changed
    from desktop_runtime.agent_status import statuses
    from desktop_runtime.owned_coding import _PROVIDER_BINARIES

    if not actor_subject:
        return None
    source = _command_evidence(text)
    named = named_coding_tools(text)
    if len(named) > 1:
        return None
    allowed = consents(actor_subject)
    tools = statuses()
    ready = {item["name"] for item in tools if item.get("name") in _PROVIDER_BINARIES
             and all(item.get(field) is True for field in ("installed", "signed_in", "available", "subscription_login"))
             and allowed.get(item["name"]) is True}

    if not named and re.search(r"\b(?:my|our|the)\s+(?:connected\s+)?coding\s+tool\b", source, re.I):
        choices = sorted(ready)
        if len(choices) > 1:
            raise HTTPException(409, detail={"code": "build_tool_choice_required", "message":
                "Which coding tool should I use: " + " or ".join(_TOOL_NAMES[name] for name in choices) + "?"})
    selected = next((provider for provider in (sorted(named) if named else ("codex", "claude", "cursor", "opencode"))
                     if provider in ready), None)
    if selected is None and source:
        raise_if_tool_changed(allowed, tools, next(iter(named), None))
    if named and selected is None:
        name = _TOOL_NAMES[next(iter(named))]
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message":
            f"{name} is not connected, signed in, or allowed; open Settings > Keys and connections > Coding tools, connect {name}, sign in with your subscription and allow Charlie, then retry your build."})
    return selected

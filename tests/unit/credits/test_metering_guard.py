"""No new direct provider dial outside a reviewed metering seam.

Existing dial sites are a bounded inventory, including explicit owner handoffs.
This guard cannot prove a generic URL variable is a paid provider; runtime tests
still prove reserve/call/settle. New aliases/imports and new modules are checked.
"""
import ast
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "services/company_os/app"
PROVIDERS = {"api.openai.com", "api.resend.com", "api.firecrawl.dev", "api.exa.ai", "api.tavily.com"}


def provider_calls(source: str) -> Counter:
    tree = ast.parse(source)
    dials = {"sole.transmit", "sole.transmit_stream", "LLMClient"}
    constants = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "llm_gateway.sole":
                    prefix = name.asname or name.name
                    dials.update({prefix + ".transmit", prefix + ".transmit_stream"})
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "llm_gateway":
                for name in node.names:
                    if name.name == "sole":
                        prefix = name.asname or name.name
                        dials.update({prefix + ".transmit", prefix + ".transmit_stream"})
            if node.module in {"llm_gateway.sole", "llm_gateway.client"}:
                dials.update(name.asname or name.name for name in node.names
                             if name.name in {"transmit", "transmit_stream", "LLMClient"})
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants.update({target.id: node.value.value for target in node.targets if isinstance(target, ast.Name)})
    def url_prefix(value):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            return constants.get(value.id)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return url_prefix(value.left)
        if isinstance(value, ast.JoinedStr) and value.values:
            return url_prefix(value.values[0])
        return None
    calls = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = ast.unparse(node.func)
        if function in dials or any(function.endswith(suffix) for suffix in (
            ".chat.completions.create", ".responses.create", ".messages.create",
            ".embeddings.create", ".audio.speech.create", ".audio.transcriptions.create",
        )):
            calls["llm"] += 1
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"post", "get", "request", "stream"}:
            values = node.args + [item.value for item in node.keywords if item.arg == "url"]
            for value in values[:2]:
                url = url_prefix(value)
                if isinstance(url, str) and urlsplit(url).hostname in PROVIDERS:
                    calls[urlsplit(url).hostname] += 1
    return calls


# Reviewed existing dials are frozen below. Counts must shrink as owner seams
# migrate; never increase one to permit an unmetered new call.
EXISTING = {
    "actuation/tools/email_send.py": {"api.resend.com": 1},  # credits.metered.call
    "api/greeting_api.py": {"api.openai.com": 1},  # R3 credits.audio_meter.speech_receipt seam
    "api/openai_api.py": {"llm": 1},  # S01/R2 token metering seam
    "api/voice_api.py": {"api.openai.com": 1},  # R3 before S02 usage seam
    "build/device_ai_step.py": {"api.openai.com": 1, "llm": 1},  # device invocation holds
    "build/device_browser_ai.py": {"api.openai.com": 1},  # existing stream: device_invocations.admit/record_result
    "build/device_vision.py": {"api.openai.com": 1},  # device invocation holds
    "builder/model_proxy.py": {"llm": 1},  # build pool
    "chat/assistant.py": {"llm": 1},  # charge_sync
    "chat/llm_router.py": {"llm": 1},  # charge_sync
    "credits/metered.py": {"llm": 1},  # shared facade
    "email/providers.py": {"api.resend.com": 1},  # platform notifications
    "firstlook/copy.py": {"llm": 1},  # charge
    "gtm/relationships/discover.py": {"api.firecrawl.dev": 1},  # R4 before S02 seam
    "gtm/supymem/draft.py": {"llm": 1},  # charge_sync
    "intake/smart.py": {"llm": 1},  # charge
    "pagebuilder/runtime.py": {"api.openai.com": 1, "llm": 1},  # journaled perform
    "planning/execute.py": {"llm": 1},  # S03 credits.metered.llm seam
    "research/dossier.py": {"llm": 1},  # R4/S09 credits.metered.llm seam
    "signals/research.py": {"api.firecrawl.dev": 1, "api.exa.ai": 1, "api.tavily.com": 1},  # R4/S09 per-request seam
    "gtm/exa_people.py": {"api.exa.ai": 1},  # LinkedIn profiles: one search per job inside metered.call
    "simulation/runtime.py": {"llm": 1},  # journaled perform
}


def test_no_new_provider_calls_outside_reviewed_metering_seams():
    unexpected = {}
    for path in APP.rglob("*.py"):
        key = path.relative_to(APP).as_posix()
        extra = provider_calls(path.read_text()) - Counter(EXISTING.get(key, {}))
        if extra:
            unexpected[key] = dict(extra)
    assert not unexpected, f"Route provider calls through app.credits.metered or journaled perform: {unexpected}"


def test_guard_fails_for_new_direct_calls_and_import_aliases():
    assert provider_calls("from llm_gateway.sole import transmit as dial\nasync def run():\n await dial([])") == {"llm": 1}
    assert provider_calls("await client.responses.create(model='synthetic')") == {"llm": 1}
    assert provider_calls("httpx.post('https://api.resend.com/emails')") == {"api.resend.com": 1}
    assert provider_calls("client.post('https://api.firecrawl.dev/v2/' + path)") == {"api.firecrawl.dev": 1}
    assert provider_calls("client.stream('POST', 'https://api.firecrawl.dev/v2/' + path)") == {"api.firecrawl.dev": 1}
    assert provider_calls("import llm_gateway.sole as door\nawait door.transmit([])") == {"llm": 1}
    assert provider_calls("await metered.llm([])\nawait metered.call(callback)") == {}


def test_converted_feature_modules_keep_shared_facade():
    for path, seam in {"gbrain/think.py": "await llm(", "gtm/localization/service.py": "await llm(",
                       "actuation/tools/email_send.py": "await call("}.items():
        assert seam in (APP / path).read_text(), f"Missing paid-call facade in {path}"

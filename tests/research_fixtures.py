"""Deterministic provider boundary fixtures; the actual runner/credits/DB run."""
import json
from types import SimpleNamespace

from tests.idea_fixtures import install_provider


def install_research(monkeypatch, *, empty=False, invalid=False, callback=None):
    # This legacy fixture covers search/review receipts. New prior-art journey
    # tests explicitly enable the phase and install its own recorded adapters.
    monkeypatch.setenv("RESEARCH_PRIOR_ART_ENABLED", "0")
    from app.research import deep, github_search
    calls = []
    class Provider:
        name = "test_search"
        async def search(self, query):
            calls.append(("search", query))
            return [] if empty else [{"url": "https://example.com/bakery", "title": "Bakery order software", "snippet": "Bakery order management. Plans cost $12 monthly. A free trial lasts 14 days."}]
    monkeypatch.setattr(deep, "_web_provider", lambda: Provider())
    async def github(query, workspace_id, limit=5):
        calls.append(("github", workspace_id))
        return {"items": [] if empty else [{"url": "https://github.com/example/bakery", "title": "example/bakery", "snippet": "An order management library.", "repository": {"full_name": "example/bakery", "private": False, "stars": 12, "license_spdx": "MIT", "license_url": "https://api.github.com/licenses/mit", "archived": False}}], "provider": "github", "credential_source": "anonymous", "incomplete": False}
    monkeypatch.setattr(github_search, "search_public_repositories", github)
    def completion(messages, options):
        calls.append(("review", options))
        if callback:
            return callback(messages, options)
        # Contract keeps sources in the final serialized user message.
        data = json.loads(messages[-1]["content"])
        sources = data["sources"]
        source = next(s for s in sources if any("$12" in text for text in s["excerpts"]))
        sid = "rsf_foreign" if invalid else source["id"]
        report = {"overview": "Evidence suggests an existing alternative needs comparison.",
            "observations": [{"source_id": sid, "quote_index": 0}],
            "assessments": [{"kind": "inference", "category": "competitor_alternative", "citations": [{"source_id": sid, "quote_index": 0}], "assessment": "This source may be an existing alternative for order intake."}],
            "service_checks": [{"source_id": sid, "pricing_quote_index": 0, "limits_quote_index": 0, "credentials_quote_index": None}],
            "coverage_gaps": ["Compare a real bakery's existing order workflow."],
            "proposed_tests": ["Ask one bakery owner whether order intake is worth paying for."]}
        from openai.types import CompletionUsage
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(report)), finish_reason="stop")],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=250, total_tokens=750,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_provider(monkeypatch, completion)
    return calls

import json
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app


GUIDE = Path(__file__).resolve().parents[3] / "apps/console/src/lib/capabilityGuide.ts"
EXPECTED = {
    "clarify": "new_venture",
    "research": "research.start",
    "customers": "gtm_review",
    "drafts": "outreach_draft",
    "experiments": "experiment_start",
    "record-results": "experiment_record",
    "page": "pagebuilder.build",
    "app-build": "build.tool.start",
    "publish": "site.publish",
}


@pytest.mark.parametrize("domain", ["industrial pump maintenance", "family hiking journals"])
def test_local_guide_examples_route_to_the_described_actions(monkeypatch, domain):
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.chat.intent import classify_intent, split_speech_acts

    declaration = GUIDE.read_text(encoding="utf-8").split("const LOCAL_PROMPTS = ", 1)[1]
    prompts, _ = json.JSONDecoder().raw_decode(declaration)
    assert prompts.keys() == EXPECTED.keys()
    for name, prompt in prompts.items():
        for wording in (prompt, f"{prompt.rstrip('.')} for my venture about {domain}."):
            assert classify_intent(wording) == EXPECTED[name], (name, wording)
            assert split_speech_acts(wording) == [(EXPECTED[name], wording)], (name, wording)

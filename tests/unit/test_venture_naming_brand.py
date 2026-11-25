import asyncio

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.intake import naming  # noqa: E402

BAD = ["I Am", "User Can", "Hey Whatever", "Instagram's Most", "Startuphelps", "Clubs Reduce", "Nearest Kiln"]
IDEAS = [
    "nearest kiln finder",
    "I am building something for gyms to reduce churn",
    "User can upload photos and share them",
    "hey whatever, an app so people find startup help",
    "Instagram's most popular posts tracker for brands",
    "startup helps founders find investors",
    "museums retain members with book recommendations",
]


@pytest.mark.parametrize("bad", BAD)
def test_validator_rejects_bad_names(bad):
    assert not naming.is_valid_name(bad)


@pytest.mark.parametrize("idea", IDEAS)
def test_fallback_never_produces_bad_names(idea):
    out = naming.derive_name(None, idea)
    assert out not in BAD and (out == "Untitled venture" or naming.is_valid_name(out)), out


def test_fallback_uses_key_nouns():
    assert naming.derive_name(None, "nearest kiln finder") == "Kiln Finder"


def test_validator_blocks_profanity_and_long():
    assert not naming.is_valid_name("Shit Finder")
    assert not naming.is_valid_name("One Two Three Four")
    assert naming.is_valid_name("KilnScout")


def test_clean_llm_name():
    assert naming.clean_llm_name('"KilnScout".') == "KilnScout"
    assert naming.clean_llm_name("I am a name") is None


def test_suggest_name_mocked_llm(monkeypatch):
    async def ok(messages, *, workspace_id):
        return "KilnScout\n"

    async def bad(messages, *, workspace_id):
        return "Hey Whatever"

    import app.chat.assistant as a

    monkeypatch.setattr(a, "_complete", ok)
    assert asyncio.run(naming.suggest_name("nearest kiln finder", "ws")) == "KilnScout"
    monkeypatch.setattr(a, "_complete", bad)
    assert asyncio.run(naming.suggest_name("nearest kiln finder", "ws")) is None

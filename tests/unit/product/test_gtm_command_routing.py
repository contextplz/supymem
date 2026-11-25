"""Explicit diagnosis commands retain their scope instead of searching it."""
import pytest
from tests.company_os_harness import load_company_os_app

@pytest.fixture
def classifier(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/routing.db")
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.chat import intent
    return intent

ACTUAL_REQUEST = (
    "Give StudioSlot a go-to-market diagnosis and the smallest next acquisition experiment. "
    "This is a concept with no product, interviews, users or validated demand. "
    "Focus on finding three independent trainers for brief non-sales interviews. "
    "Use anonymized verbal workflow descriptions, not client chats or contact lists. "
    "Nothing has been sent."
)

@pytest.mark.parametrize("prompt", [ACTUAL_REQUEST,
    "Please give me a GTM diagnosis. Find three trainers later; do not send anything.",
    "Generate a go-to-market diagnosis for StudioSlot. Pricing remains unknown.",
    "Prepare my GTM diagnosis using the saved research.",
    "Can you create a GTM diagnosis? Nothing is to be published."])
def test_diagnosis_commands_use_reviewed_route_and_preserve_context(prompt, classifier):
    assert classifier.classify_intent(prompt) == "gtm_review"
    assert classifier.split_speech_acts(prompt) == [("gtm_review", prompt)]

@pytest.mark.parametrize("prompt", [
    "Give me an explanation of a GTM diagnosis",
    "How do I prepare a GTM diagnosis?",
    "Do not generate a GTM diagnosis; explain the saved research",
    "Find articles about go-to-market diagnosis",
    "Give me a summary of my saved GTM diagnosis",
    "Give StudioSlot a landing page, not a go-to-market diagnosis",
])
def test_readonly_questions_and_negation_do_not_create_diagnosis_quotes(prompt, classifier):
    assert classifier.classify_intent(prompt) != "gtm_review"

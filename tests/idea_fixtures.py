"""Provider network fixture only; production parsing, metering and persistence stay real."""
from __future__ import annotations
import json
import os
from types import SimpleNamespace


def completion(*, proposal=True, case="know", understanding=None, text="Who would use this first?",
               options=None, confidence=None, message=None):
    data = {"case": case, "action": "proposal" if proposal else "question",
            "message": message or "An order intake board for neighbourhood bakeries; validate with one owner.",
            "understanding": understanding or {"summary": "Bakery order intake board", "offer": "Order intake board"},
            "question": None if proposal else {"text": text, "field": "customer", "why": "Choose an accessible first customer", "options": options or ["Bakery owner", "Not sure"]}}
    if confidence is not None:
        data["confidence"] = confidence
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100, total_tokens=300), model="DeepSeek-V4-Pro")


def questions_completion():
    data = {"questions": [
        {"id": "Q1", "question": "Has any bakery paid or begged for an order board?", "options": ["One pays already", "Only interested"]},
        {"id": "Q2", "question": "How do bakeries track custom cake orders today?", "options": ["Email and paper", "A spreadsheet"]}]}
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=300, completion_tokens=100, total_tokens=400), model="DeepSeek-V4-Pro")


def validation_completion(messages):
    data = json.loads(messages[-1]["content"])
    rid = data["founder_revision_source"]["id"]
    methods = data["requested_methods"]
    report = {
        "verdict": "needs_evidence",
        "summary": "The idea remains a founder hypothesis until a customer test supplies demand evidence.",
        "findings": [
            {
                "method": method,
                "assessment": (
                    "No scoped company memory was available for this review."
                    if method == "gbrain_think"
                    else "The founder described a problem, but no observed demand was supplied."
                ),
                "source_ids": [] if method == "gbrain_think" else [rid],
            }
            for method in methods
        ],
        "gaps": ["No customer payment evidence was supplied."],
        "proposed_tests": [
            {
                "hypothesis": "A reachable customer will pay to solve this problem.",
                "action": "Ask the first reachable customer for a paid pilot.",
                "success_measure": "The customer agrees to pay.",
            }
        ],
        "questions": ["Who is the first reachable customer?"],
        "forcing_questions": [
            {
                "question_id": answer["id"],
                "question": answer["question"],
                "founder_answer": "",  # The parser binds the saved answer server-side.
                "assessment": (
                    "This unanswered question remains an unverified assumption."
                    if answer.get("assumed")
                    else "The saved answer is founder testimony, not verified demand."
                ),
                "risk": "No customer payment evidence was supplied.",
                "suggested_test": "Ask the first reachable customer for a paid pilot.",
            }
            for answer in data.get("gstack_answers", [])
        ],
    }
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(report)), finish_reason="stop"
        )],
        usage=SimpleNamespace(prompt_tokens=500, completion_tokens=300, total_tokens=800),
        model="DeepSeek-V4-Pro",
    )


def install_provider(monkeypatch, callback=None):
    from llm_gateway import sole
    from app.core import providers_local
    from app.core.local_mode import is_local
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_BASE_URL", "https://foundry.example.test/openai/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-provider-key")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Pro")
    if is_local():
        for name in ("SUPYMEM_LLM_PROVIDER", *providers_local.PROVIDER_ENV["azure_openai"],
                     *(f"SUPYMEM_MODEL_{role.upper()}" for role in providers_local.ROLES)):
            monkeypatch.delenv(name, raising=False)
        providers_local.save_settings(provider="azure_openai", base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"], models={"chat": os.environ["LLM_DEPLOYMENT_PRO"],
                "planning": os.environ["LLM_DEPLOYMENT_PRO"], "extraction": os.environ["LLM_DEPLOYMENT_FLASH"]})
    calls = []
    async def primary(messages, **kwargs):
        calls.append((messages, kwargs))
        if callback:
            return callback(messages, kwargs)
        if "short GStack office-hours session" in messages[0]["content"]:
            return questions_completion()
        if kwargs.get("purpose") == "idea.validate":
            return validation_completion(messages)
        snapshot = json.loads(messages[-1]["content"])
        return completion(proposal=snapshot["force_propose"], case=snapshot.get("explicit_case") or "know", understanding=snapshot["understanding"])
    monkeypatch.setattr(sole, "_primary_chat", primary)
    if is_local():
        monkeypatch.setattr(sole, "byok_chat_completion", primary)
    return calls

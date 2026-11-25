from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.clarify.generator import generate_questions, product_referent
from app.clarify.slots import slots_for_budget


def test_product_referent_comprehends_first_person_rant():
    raw = (
        "I want to build a fully agencitc EoR system, offering it at 3k INR per "
        "employee so companies can hire anywhere"
    )
    phrase = product_referent(idea=raw)
    assert "EoR" in phrase or "EOR" in phrase
    assert "I want" not in phrase
    assert "…" not in phrase
    assert product_referent(idea=raw, name="Paygrid") == "Paygrid"


def test_refresh_rewrites_chopped_idea_dump():
    from app.clarify.generator import refresh_question_copy

    raw = "I want to build a fully agencitc EoR system, offering it at 3k INR per employee"
    stale = [
        {
            "id": "qst_1",
            "slot_key": "goal_primary",
            "prompt": (
                "What's the strongest evidence someone actually wants "
                "“I want to build a fully agencitc EoR system, offering it at 3k INR per…”"
            ),
            "why_asked": "old",
        }
    ]
    out = refresh_question_copy(stale, idea=raw)
    assert out[0]["id"] == "qst_1"
    assert "EoR" in out[0]["prompt"]
    assert "I want to build" not in out[0]["prompt"]
    assert "…" not in out[0]["prompt"]


def test_questions_mention_idea_and_competitors():
    idea = "AI Sales Meeting Agent that drafts follow-ups after Zoom calls"
    research = {
        "competitors": ["Fireflies", "Otter"],
        "demand": "mixed",
        "queries": ["AI meeting notes competitors"],
    }
    qs = generate_questions(slots_for_budget(4), idea=idea, research=research)
    assert len(qs) == 5
    assert any(q["slot_key"] == "llm_runtime" for q in qs)
    blob = " ".join(q["prompt"] + " " + q["why_asked"] for q in qs)
    assert "AI Sales Meeting Agent" in blob
    assert "I want" not in blob
    assert "…" not in blob
    keys = [q["slot_key"] for q in qs]
    assert "mvp_scope" in keys
    assert "market_reach" in keys or "audience" in keys
    market = next((q for q in qs if q["slot_key"] == "market_reach"), None)
    if market:
        assert "Fireflies" in market["prompt"] or "Fireflies" in (market["options"][2].get("label") or "")

"""Network-only plan fixture; schema, credits, scope and persistence stay real."""
import json
from types import SimpleNamespace

from tests.idea_fixtures import install_provider


def proposal(context):
    kinds = ["landing", "validation", "core", "monetisation", "acquisition", "first_users", "feedback", "iteration", "first_paid_user"]
    capabilities = ["build.run", "founder.action", "build.run", "founder.action", "founder.action", "founder.action", "founder.action", "app.iterate", "founder.action"]
    milestones = []
    for index, (kind, capability) in enumerate(zip(kinds, capabilities, strict=True)):
        milestones.append({"key": f"m{index+1}", "kind": kind, "title": f"Bakery order board: {kind}",
            "description": "Test the order intake offer with a neighbourhood bakery.", "included": True,
            "depends_on": [] if not index else [f"m{index}"],
            "acceptance": ["Founder reviews an observed result from one bakery owner."],
            "tasks": [{"key": "t1", "title": f"Review {kind} for one bakery", "description": "Use a concrete bakery order intake test and record the result.",
                "capability_id": capability, "implementation": "founder_action" if capability == "founder.action" else "new_code",
                "source_ids": [context["sources"][0]["id"]] if context["sources"] else [],
                "resource_ids": [context["assets"][0]["id"]] if context["assets"] else [],
                "acceptance": ["Record the bakery owner's decision and the next experiment."],
                "founder_action": {"title": "Review with one bakery", "steps": ["Choose one bakery owner you can reach.", "Request permission to test the offer."],
                    "verification": "Record a paid invoice or rejection; do not assume demand."} if capability == "founder.action" else None,
                "landing_expectations": ({
                    "contract": {"profile": "static_landing_v1", "product_name": "Bakery order board",
                        "required_text": ["Record the bakery owner's decision and the next experiment."], "actions": []},
                    "criteria": [{"criterion": 0, "summary": "Confirm the reviewed offer is visible.",
                        "text_indices": [0], "action_ids": []}],
                } if kind == "landing" else None)}]})
    if context.get("managed_conversion"):
        expected = milestones[0]["tasks"][0]["landing_expectations"]
        expected["contract"]["actions"] = [{"id": "early_access", "kind": "external_link", "role": "link",
            "name": "Request early access", "page": "index.html", "target": context["managed_conversion"]["url"], "depends_on": []}]
        expected["criteria"][0]["action_ids"] = ["early_access"]
    return {"summary": "Test a narrow order intake board and paid offer with one bakery.",
        "goal_first_paid_user": "One bakery pays for a completed order intake trial.", "target_days": None,
        "assumptions": ["Founder can reach one bakery."], "exclusions": ["Native mobile applications"],
        "evidence_gaps": ["Willingness to pay is untested."], "milestones": milestones}


def install_planner(monkeypatch, callback=None):
    def complete(messages, options):
        context = json.loads(messages[-1]["content"])
        value = callback(context, options) if callback else proposal(context)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=1600, total_tokens=2500), model="DeepSeek-V4-Pro")
    return install_provider(monkeypatch, complete)


def seed_research(d):
    """Already-observed test evidence; never loaded by application code."""
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from core.clock import utcnow
    from core.ids import new_id
    did, fid = new_id("rsd"), new_id("rsf")
    with d["factory"]() as session:
        session.add(ResearchDossier(id=did, workspace_id=d["wid"], venture_id=d["vid"], revision_id=d["rid"],
            kind="deep", state="partial", budget_calls=9, calls_used=9,
            summary=json.dumps({"review_kind": "model_assessment", "review": {"coverage_gaps": ["Demand untested"]}, "coverage_gaps": ["Demand untested"]}), created_at=utcnow(), completed_at=utcnow()))
        session.flush()
        session.add(ResearchFinding(id=fid, dossier_id=did, workspace_id=d["wid"], venture_id=d["vid"], category="competitor_alternative",
            title="Order board", url="https://example.com/bakery", source="test_search", summary="Bakery order management.",
            evidence=json.dumps({"observed_at": utcnow().isoformat(), "snippet": "Bakery order management."}), created_at=utcnow()))
        session.commit()
    return did, fid

"""One plain-language verdict for a finished research dossier, derived only from saved fields.

Search leads never count as proof: a thin report yields "Proceed with a test first",
never a claim that the idea fails. The founder can always continue to planning.
"""

from __future__ import annotations

from app.research.pricing import pricing_evidence_quote

_AREAS = {"problem_validation": "the problem", "customer_demand": "customer demand", "competition": "competition",
          "pricing": "advertised offer pricing", "willingness_to_pay": "willingness to pay", "market_size": "market size", "risks": "risks"}
_TITLES = {"proceed": "Recommended to proceed", "test_first": "Proceed with a test first", "not_yet": "Not yet recommended",
           "no_sources": "Research could not find sources"}


def _join(names: list[str]) -> str:
    return ", ".join(names[:3])


def qualified_count(findings: dict) -> int:
    return sum(1 for group in findings.values() for f in group
               if f.get("evidence_kind") not in {"search_lead_unverified", "name_source"} and f.get("category") != "review_lead"
               and f.get("founder_flag") != "exclude")


def _verified_passages(findings: dict) -> dict[str, list[str]]:
    """Only admitted, non-excluded server-fetched page passages support facts."""
    passages = {}
    for group in findings.values():
        for finding in group:
            if (finding.get("page_verified") is not True or not finding.get("url")
                    or finding.get("founder_flag") == "exclude"
                    or finding.get("category") == "review_lead"
                    or finding.get("evidence_kind") in {"search_lead_unverified", "name_source"}):
                continue
            texts = [" ".join(row["text"].split())
                     for row in finding.get("additional_excerpts") or []
                     if row.get("verified_page") is True and isinstance(row.get("text"), str)
                     and row["text"].strip()]
            if texts:
                passages.setdefault(finding["url"], []).extend(texts)
    return passages


def _grounded(entry: dict, passages: dict) -> bool:
    text = " ".join(str(entry.get("finding") or "").split())
    return bool(entry.get("verified") is True and text
                and any(text in passage for passage in passages.get(entry.get("url"), [])))


def research_verdict(state: str, findings: dict, dimensions: dict | None, review: dict | None,
                     strengths: list[str] | None = None, *, has_customers: bool = False) -> dict:
    sources = qualified_count(findings)
    leads = sum(len(g) for g in findings.values()) - sources
    if state not in {"complete", "partial"}:
        verdict, title, next_step = {
            "failed": ("failed", "Research didn't finish", "Run research again."),
            "outdated": ("outdated", "Research is outdated", "Run research again for your current idea."),
        }.get(state, ("in_progress", "Researching…", ""))
        return {"verdict": verdict, "title": title, "why": [], "why_not": [], "next_step": next_step,
                "source_count": sources, "lead_count": leads}
    passages = _verified_passages(findings)
    dims = {k: (v or {}).get("status") for k, v in (dimensions or {}).items() if k in _AREAS}
    support = [_AREAS[k] for k, s in dims.items() if s in {"supporting_leads", "mixed_leads"}
               and any(_grounded(entry, passages)
                       and (k != "pricing" or pricing_evidence_quote(entry.get("finding")))
                       for entry in dimensions[k].get("support") or [])]
    challenge = [_AREAS[k] for k, s in dims.items() if s in {"challenging_leads", "mixed_leads"}
                 and any(_grounded(entry, passages) for entry in dimensions[k].get("challenge") or [])]
    unknown = [_AREAS[k] for k in dims if _AREAS[k] not in support and _AREAS[k] not in challenge]
    if len(challenge) > len(support):
        verdict = "not_yet"
    elif len(support) >= 2 and not challenge and len(passages) >= 3:
        verdict = "proceed"
    else:
        verdict = "test_first"

    # A category label alone does not establish an offering, let alone buyers.
    verified_ids = {f.get("id"): f.get("url") for group in findings.values() for f in group
                    if f.get("url") in passages and f.get("founder_flag") != "exclude"}
    competitors = any(
        check.get("product_quote") and any(" ".join(str(check["product_quote"]).split()) in passage
            for passage in passages.get(verified_ids.get(check.get("source_id")), []))
        for check in (review or {}).get("competitor_checks") or [])
    why, why_not = [], []
    # The founder's own idea review is the most specific reason; quote it before generic ones.
    why.extend(f"Your idea review found: {str(x).strip().rstrip('. ')}." for x in (strengths or [])[:2] if str(x).strip())
    if support:
        why.append(f"Verified public source passages cover {_join(support)}; customer validation is still needed.")
    if competitors:
        why.append("Verified sources describe similar offerings; this does not establish sales or willingness to pay.")
    if not why:
        why.append("Planning is cheap and you can change course after it.")
    if challenge:
        why_not.append(f"Some sources push back on {_join(challenge)}.")
    if unknown:
        why_not.append(f"No strong public evidence yet on {_join(unknown)}.")
    if competitors:
        why_not.append("Existing products mean you need a clear reason people will choose yours.")
    if sources < 3:
        why_not.append("Few sources were relevant enough to count as evidence.")
    why_not.append("Public search cannot show real demand; only customers can.")
    next_step = {
        "proceed": "Continue to the plan, and check with a few real customers as you build.",
        "test_first": "Continue to the plan, and first ask five target customers about a recent example of this problem.",
        "not_yet": "Continue to the plan if you still believe in the idea, or tighten the idea first.",
    }[verdict]
    if sources == 0:
        # Never present a verdict as research when nothing was found.
        verdict = "no_sources"
        why, why_not = [], ["Web search returned no usable sources for this idea, so there is no research behind any verdict.",
                            "Public search cannot show real demand; only customers can."]
        next_step = "Rerun research, or tighten the idea wording; you can still continue to the plan and check with real customers."
    title = _TITLES[verdict]
    if has_customers:
        title = "Grow what's already working"
        why = ["Your existing paying customers are demand evidence from your own experience.",
               *(line for line in why if "customer validation is still needed" not in line)]
        why_not = ["The next acquisition channel is still unproven; test it with a small cohort before scaling.",
                   *(line for line in why_not if not line.startswith(("No strong public evidence", "Public search cannot show real demand")))]
        next_step = "Find customers by testing one repeatable channel, and learn from retention and replies."
    return {"verdict": verdict, "title": title, "why": why[:3], "why_not": why_not[:3], "next_step": next_step,
            "source_count": sources, "lead_count": leads}

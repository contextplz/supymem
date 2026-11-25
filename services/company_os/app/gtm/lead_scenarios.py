"""Lab lead-generation scenarios — varied ICP / geo / role queries without outbound."""

from __future__ import annotations

import random
from typing import Any

# Preset “situations” for Marketing Lab / AI GTM generate.
SCENARIO_PRESETS: list[dict[str, Any]] = [
    {
        "id": "sf_ai_founders",
        "label": "SF AI founders",
        "industry": "AI",
        "role": "Founder",
        "company_size": "1-50",
        "geo": "San Francisco",
        "keyword": "AI GTM",
        "query": "AI founders in San Francisco",
    },
    {
        "id": "eu_saas_cmo",
        "label": "EU SaaS CMOs",
        "industry": "B2B SaaS",
        "role": "CMO",
        "company_size": "11-200",
        "geo": "Berlin",
        "keyword": "outbound",
        "query": "B2B SaaS CMOs in Berlin",
    },
    {
        "id": "india_fintech_gtm",
        "label": "India fintech GTM",
        "industry": "Fintech",
        "role": "Head of GTM",
        "company_size": "11-100",
        "geo": "Bangalore",
        "keyword": "payments",
        "query": "Fintech Head of GTM in Bangalore",
    },
    {
        "id": "nyc_devtools_ceo",
        "label": "NYC devops CEOs",
        "industry": "Developer Tools",
        "role": "CEO",
        "company_size": "1-50",
        "geo": "New York",
        "keyword": "infra",
        "query": "Developer Tools CEOs in New York",
    },
    {
        "id": "london_agency_founder",
        "label": "London agency founders",
        "industry": "Marketing Agency",
        "role": "Founder",
        "company_size": "1-50",
        "geo": "London",
        "keyword": "growth",
        "query": "marketing agency founders in London",
    },
    {
        "id": "austin_sales_ops",
        "label": "Austin sales ops",
        "industry": "B2B SaaS",
        "role": "VP Sales",
        "company_size": "50-500",
        "geo": "Austin",
        "keyword": "pipeline",
        "query": "B2B SaaS VP Sales in Austin",
    },
    {
        "id": "singapore_hrtech",
        "label": "Singapore HR tech",
        "industry": "HR Tech",
        "role": "Founder",
        "company_size": "1-100",
        "geo": "Singapore",
        "keyword": "workforce",
        "query": "HR Tech founders in Singapore",
    },
    {
        "id": "toronto_healthtech",
        "label": "Toronto healthtech",
        "industry": "Healthtech",
        "role": "CPO",
        "company_size": "11-200",
        "geo": "Toronto",
        "keyword": "clinical",
        "query": "Healthtech CPOs in Toronto",
    },
    {
        "id": "latam_saas",
        "label": "LatAm SaaS founders",
        "industry": "B2B SaaS",
        "role": "Founder",
        "company_size": "1-50",
        "geo": "São Paulo",
        "keyword": "LATAM",
        "query": "B2B SaaS founders in São Paulo",
    },
    {
        "id": "legaltech_us",
        "label": "US legaltech",
        "industry": "Legal",
        "role": "Founder",
        "company_size": "1-100",
        "geo": "United States",
        "keyword": "contracts",
        "query": "Legal tech founders in United States",
    },
]

_INDUSTRIES = (
    "AI",
    "B2B SaaS",
    "Fintech",
    "Developer Tools",
    "Marketing Agency",
    "HR Tech",
    "Healthtech",
    "Legal",
    "Climate",
    "Edtech",
)
_ROLES = (
    "Founder",
    "CEO",
    "CMO",
    "Head of GTM",
    "VP Sales",
    "CPO",
    "Head of Growth",
)
_SIZES = ("1-10", "1-50", "11-100", "11-200", "50-500", "100-1000")
_GEOS = (
    "San Francisco",
    "New York",
    "Austin",
    "London",
    "Berlin",
    "Amsterdam",
    "Bangalore",
    "Singapore",
    "Toronto",
    "São Paulo",
)
_KEYWORDS = (
    "outbound",
    "pipeline",
    "AI GTM",
    "growth",
    "sales engagement",
    "PLG",
    "enterprise",
    "SMB",
)


def list_presets() -> list[dict[str, Any]]:
    return [dict(p) for p in SCENARIO_PRESETS]


def build_query(
    *,
    industry: str | None = None,
    role: str | None = None,
    company_size: str | None = None,
    geo: str | None = None,
    keyword: str | None = None,
    query: str | None = None,
) -> str:
    if query and query.strip():
        return query.strip()[:200]
    bits = [
        (industry or "").strip() or None,
        (role or "").strip() or "founders",
        f"at {company_size} companies" if (company_size or "").strip() else None,
        f"in {(geo or '').strip()}" if (geo or "").strip() else None,
        (keyword or "").strip() or None,
    ]
    return " ".join(b for b in bits if b)[:200] or "B2B SaaS founders"


def resolve_scenario(
    *,
    scenario_id: str | None = None,
    randomize: bool = False,
    industry: str | None = None,
    role: str | None = None,
    company_size: str | None = None,
    geo: str | None = None,
    keyword: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Return a concrete scenario dict used for /ai-gtm/lab/generate."""
    if randomize and not scenario_id:
        # Mix: 60% pick a named preset, 40% fully random params.
        if random.random() < 0.6:
            base = dict(random.choice(SCENARIO_PRESETS))
            base["mode"] = "preset_random"
            return base
        industry = random.choice(_INDUSTRIES)
        role = random.choice(_ROLES)
        company_size = random.choice(_SIZES)
        geo = random.choice(_GEOS)
        keyword = random.choice(_KEYWORDS)
        q = build_query(
            industry=industry,
            role=role,
            company_size=company_size,
            geo=geo,
            keyword=keyword,
        )
        return {
            "id": "random",
            "label": f"Random · {industry} · {geo}",
            "industry": industry,
            "role": role,
            "company_size": company_size,
            "geo": geo,
            "keyword": keyword,
            "query": q,
            "mode": "fully_random",
        }

    if scenario_id:
        for p in SCENARIO_PRESETS:
            if p["id"] == scenario_id:
                out = dict(p)
                # Allow overrides on top of preset
                if industry:
                    out["industry"] = industry
                if role:
                    out["role"] = role
                if company_size:
                    out["company_size"] = company_size
                if geo:
                    out["geo"] = geo
                if keyword:
                    out["keyword"] = keyword
                if query:
                    out["query"] = query.strip()[:200]
                else:
                    out["query"] = build_query(
                        industry=out.get("industry"),
                        role=out.get("role"),
                        company_size=out.get("company_size"),
                        geo=out.get("geo"),
                        keyword=out.get("keyword"),
                        query=out.get("query"),
                    )
                out["mode"] = "preset"
                return out

    q = build_query(
        industry=industry,
        role=role,
        company_size=company_size,
        geo=geo,
        keyword=keyword,
        query=query,
    )
    return {
        "id": "custom",
        "label": "Custom",
        "industry": (industry or "B2B").strip() or "B2B",
        "role": (role or "Founder").strip() or "Founder",
        "company_size": (company_size or "").strip() or None,
        "geo": (geo or "").strip() or None,
        "keyword": (keyword or "").strip() or None,
        "query": q,
        "mode": "custom",
    }

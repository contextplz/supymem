"""Specialist agents the GTM God may spawn. LinkedIn send and harvest are forbidden."""

from __future__ import annotations

FORBIDDEN_KINDS = frozenset(
    {
        "harvest_leads",
        "discover_leads",
        "linkedin_send",
        "linkedin_scrape",
        "unipile_send",
    }
)

AGENTS: dict[str, dict[str, str]] = {
    "talk_in_person": {
        "kind": "talk_in_person",
        "owner": "field_agent",
        "skill": "talk-to-users",
        "mode": "human",
    },
    "research_icp": {
        "kind": "research_icp",
        "owner": "icp_researcher",
        "skill": "who-is-this-for",
        "mode": "auto",
    },
    "competitors": {
        "kind": "competitors",
        "owner": "competitor_scout",
        "skill": "market-scan",
        "mode": "auto",
    },
    "positioning": {
        "kind": "positioning",
        "owner": "positioning_agent",
        "skill": "positioning-and-story",
        "mode": "auto",
    },
    "brand_voice": {
        "kind": "brand_voice",
        "owner": "brand_agent",
        "skill": "value-prop-that-converts",
        "mode": "auto",
    },
    "site": {
        "kind": "site",
        "owner": "site_composer",
        "skill": "the-homepage",
        "mode": "auto",
    },
    "domain": {
        "kind": "domain",
        "owner": "domain_agent",
        "skill": "the-homepage",
        "mode": "auto_then_human",
    },
    "launch_site": {
        "kind": "launch_site",
        "owner": "launch_agent",
        "skill": "launch-it",
        "mode": "auto",
    },
    "launch_posts": {
        "kind": "launch_posts",
        "owner": "content_agent",
        "skill": "founder-led-content",
        "mode": "auto_then_human",
    },
    "seo": {
        "kind": "seo",
        "owner": "seo_agent",
        "skill": "founder-led-content",
        "mode": "auto",
    },
    "mailbox": {
        "kind": "mailbox",
        "owner": "mailbox_agent",
        "skill": "founder-led-sales",
        "mode": "auto_then_human",
    },
    "prospects": {
        "kind": "prospects",
        "owner": "prospect_agent",
        "skill": "first-50-users",
        "mode": "human",
    },
    "outreach_drafts": {
        "kind": "outreach_drafts",
        "owner": "draft_agent",
        "skill": "founder-led-sales",
        "mode": "auto_then_human",
    },
    "review": {
        "kind": "review",
        "owner": "reviewer",
        "skill": "review-the-work",
        "mode": "auto",
    },
}

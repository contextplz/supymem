"""Official Google Ads API adapter — create PAUSED only. Never Ads Manager GUI."""

from __future__ import annotations

import os
from typing import Any

REQUIRED_ENV = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
)


def missing_env() -> list[str]:
    return [k for k in REQUIRED_ENV if not (os.environ.get(k) or "").strip()]


def available() -> dict[str, Any]:
    miss = missing_env()
    return {
        "ok": not miss,
        "missing": miss,
        "note": "Campaigns always created PAUSED. Browser Ads Manager is not used.",
    }


def create_paused_campaign(
    *,
    name: str,
    daily_budget: str | None = None,
    campaign_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    miss = missing_env()
    if miss:
        return {
            "ok": False,
            "status": "blocked",
            "error": "google_ads_credentials_missing",
            "block_reason": (
                "NeedsYou: set "
                + ", ".join(miss)
                + ". Use a test MCC. We will not click ads.google.com."
            ),
            "result": available(),
        }

    try:
        from google.ads.googleads.client import GoogleAdsClient  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "status": "blocked",
            "error": "google_ads_sdk_missing",
            "block_reason": (
                "NeedsYou: install google-ads (pip install google-ads) and configure "
                + ", ".join(REQUIRED_ENV)
                + ". No fake campaign was created."
            ),
            "result": available(),
        }

    customer_id = (
        (os.environ.get("GOOGLE_ADS_CUSTOMER_ID") or "").replace("-", "").strip()
    )
    budget_micros = int(float(daily_budget or "10") * 1_000_000)
    try:
        client = GoogleAdsClient.load_from_dict(
            {
                "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"].strip(),
                "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"].strip(),
                "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"].strip(),
                "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"].strip(),
                "use_proto_plus": True,
            }
        )
        budget_service = client.get_service("CampaignBudgetService")
        campaign_service = client.get_service("CampaignService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget_resp = budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_op]
        )
        budget_rn = budget_resp.results[0].resource_name

        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = (
            client.enums.AdvertisingChannelTypeEnum.SEARCH
        )
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.campaign_budget = budget_rn
        campaign.manual_cpc.enhanced_cpc_enabled = False
        resp = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_op]
        )
        resource = resp.results[0].resource_name
        hierarchy: dict[str, str] = {}
        if campaign_spec:
            hierarchy = _create_paused_search_ad(
                client=client,
                customer_id=customer_id,
                campaign_resource=resource,
                campaign_spec=campaign_spec,
            )
        return {
            "ok": True,
            "status": "done",
            "result": {
                "platform": "google_ads",
                "status": "PAUSED",
                "name": name,
                "resource_name": resource,
                "daily_budget": daily_budget or "10",
                "customer_id": customer_id,
                **hierarchy,
            },
            "proof": {
                "mode": "api",
                "platform": "google_ads",
                "status": "PAUSED",
                "external_id": resource,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "error": "google_ads_mutate_failed",
            "block_reason": f"NeedsYou: Google Ads API mutate failed: {exc}"[:400],
            "result": {"exception": str(exc)[:300]},
        }


def _create_paused_search_ad(
    *,
    client: Any,
    customer_id: str,
    campaign_resource: str,
    campaign_spec: dict[str, Any],
) -> dict[str, str]:
    """Create an ad group, keywords, and responsive ad beneath a paused campaign."""
    creative = campaign_spec.get("creative") or {}
    landing_url = str(campaign_spec.get("landing_url") or "")
    headlines = [str(value) for value in creative.get("google_headlines") or []]
    descriptions = [str(value) for value in creative.get("google_descriptions") or []]
    keywords = [str(value) for value in campaign_spec.get("keywords") or []]
    if not landing_url or len(headlines) < 3 or len(descriptions) < 2 or not keywords:
        raise ValueError("campaign_spec is missing landing URL, copy, or keywords")

    ad_group_service = client.get_service("AdGroupService")
    ad_group_op = client.get_type("AdGroupOperation")
    ad_group = ad_group_op.create
    ad_group.name = f"{campaign_spec.get('name', 'Campaign')} ad group"[:255]
    ad_group.campaign = campaign_resource
    ad_group.status = client.enums.AdGroupStatusEnum.PAUSED
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.cpc_bid_micros = 1_000_000
    ad_group_resp = ad_group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[ad_group_op]
    )
    ad_group_resource = ad_group_resp.results[0].resource_name

    criterion_service = client.get_service("AdGroupCriterionService")
    criterion_ops = []
    for keyword in keywords[:8]:
        criterion_op = client.get_type("AdGroupCriterionOperation")
        criterion = criterion_op.create
        criterion.ad_group = ad_group_resource
        criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED
        criterion.keyword.text = keyword[:80]
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
        criterion_ops.append(criterion_op)
    criterion_service.mutate_ad_group_criteria(
        customer_id=customer_id, operations=criterion_ops
    )

    ad_service = client.get_service("AdGroupAdService")
    ad_op = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_op.create
    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
    ad_group_ad.ad.final_urls.append(landing_url)
    for text in headlines[:15]:
        asset = client.get_type("AdTextAsset")
        asset.text = text[:30]
        ad_group_ad.ad.responsive_search_ad.headlines.append(asset)
    for text in descriptions[:4]:
        asset = client.get_type("AdTextAsset")
        asset.text = text[:90]
        ad_group_ad.ad.responsive_search_ad.descriptions.append(asset)
    ad_resp = ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_op]
    )
    return {
        "ad_group_resource_name": ad_group_resource,
        "ad_resource_name": ad_resp.results[0].resource_name,
    }


def enable_campaign(*, resource_name: str) -> dict[str, Any]:
    """Enable is spend_money — never auto-run without human floor approval."""
    return {
        "ok": False,
        "status": "blocked",
        "error": "spend_money_required",
        "block_reason": (
            "NeedsYou: enabling a Google Ads campaign spends money "
            "(floor: spend_money). "
            f"Approve in console, then POST /marketing/ads/enable with resource_name="
            f"{resource_name or '(set)'}."
        ),
        "result": {"resource_name": resource_name, "floor": "spend_money"},
    }

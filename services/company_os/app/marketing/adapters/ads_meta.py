"""Official Meta Marketing API adapter — create PAUSED only. Never Ads Manager GUI."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REQUIRED_ENV = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID")


def _token() -> str:
    return (
        os.environ.get("META_ACCESS_TOKEN") or os.environ.get("FB_ACCESS_TOKEN") or ""
    ).strip()


def missing_env(*, require_page: bool = True) -> list[str]:
    miss: list[str] = []
    if not _token():
        miss.append("META_ACCESS_TOKEN")
    if not (os.environ.get("META_AD_ACCOUNT_ID") or "").strip():
        miss.append("META_AD_ACCOUNT_ID")
    if require_page and not (os.environ.get("META_PAGE_ID") or "").strip():
        miss.append("META_PAGE_ID")
    return miss


def available() -> dict[str, Any]:
    miss = missing_env(require_page=True)
    return {
        "ok": not miss,
        "missing": miss,
        "note": "Campaigns always created PAUSED. Ads Manager GUI is not used.",
    }


def _account_id() -> str:
    raw = (os.environ.get("META_AD_ACCOUNT_ID") or "").strip()
    if raw and not raw.startswith("act_"):
        return f"act_{raw}"
    return raw


def create_paused_campaign(
    *,
    name: str,
    daily_budget: str | None = None,
    campaign_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    miss = missing_env(require_page=bool(campaign_spec))
    if miss:
        return {
            "ok": False,
            "status": "blocked",
            "error": "meta_ads_credentials_missing",
            "block_reason": (
                "NeedsYou: set "
                + ", ".join(miss)
                + " on a Business Manager system user. We will not click Ads Manager."
            ),
            "result": available(),
        }

    # Prefer facebook_business SDK when installed; else Graph HTTP with explicit mutate.
    try:
        from facebook_business.adobjects.ad import Ad  # type: ignore
        from facebook_business.adobjects.adcreative import AdCreative  # type: ignore
        from facebook_business.adobjects.adset import AdSet  # type: ignore
        from facebook_business.adobjects.campaign import Campaign  # type: ignore
        from facebook_business.api import FacebookAdsApi  # type: ignore

        FacebookAdsApi.init(access_token=_token())
        account = _account_id()
        params = {
            Campaign.Field.name: name,
            Campaign.Field.objective: str(
                (campaign_spec or {}).get("objective") or "OUTCOME_TRAFFIC"
            ),
            Campaign.Field.status: Campaign.Status.paused,
            Campaign.Field.special_ad_categories: [],
        }
        campaign = Campaign(parent_id=account)
        campaign.update(params)
        campaign.remote_create(params=params)
        cid = campaign.get_id()
        hierarchy: dict[str, str] = {}
        if campaign_spec:
            hierarchy = _create_paused_meta_ad(
                account=account,
                campaign_id=str(cid),
                name=name,
                daily_budget=daily_budget,
                campaign_spec=campaign_spec,
                classes=(AdSet, AdCreative, Ad),
            )
        return {
            "ok": True,
            "status": "done",
            "result": {
                "platform": "meta_ads",
                "status": "PAUSED",
                "name": name,
                "campaign_id": cid,
                "ad_account": account,
                **hierarchy,
            },
            "proof": {
                "mode": "api",
                "platform": "meta_ads",
                "status": "PAUSED",
                "external_id": str(cid),
            },
        }
    except ImportError:
        pass
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "error": "meta_ads_sdk_mutate_failed",
            "block_reason": f"NeedsYou: Meta Ads SDK mutate failed: {exc}"[:400],
            "result": {"exception": str(exc)[:300]},
        }

    # Graph API fallback — real mutate, never fake done.
    account = _account_id()
    data = {
        "name": name,
        "objective": str((campaign_spec or {}).get("objective") or "OUTCOME_TRAFFIC"),
        "status": "PAUSED",
        "special_ad_categories": "[]",
        "access_token": _token(),
    }
    url = f"https://graph.facebook.com/v19.0/{account}/campaigns"
    req = Request(
        url,
        data=urlencode(data).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        cid = body.get("id")
        if not cid:
            return {
                "ok": False,
                "status": "blocked",
                "error": "meta_ads_no_id",
                "block_reason": (
                    "NeedsYou: Meta Graph returned no campaign id. "
                    f"Response: {json.dumps(body)[:240]}"
                ),
                "result": body,
            }
        hierarchy: dict[str, str] = {}
        if campaign_spec:
            hierarchy = _create_paused_meta_ad_graph(
                account=account,
                campaign_id=str(cid),
                name=name,
                daily_budget=daily_budget,
                campaign_spec=campaign_spec,
            )
        return {
            "ok": True,
            "status": "done",
            "result": {
                "platform": "meta_ads",
                "status": "PAUSED",
                "name": name,
                "campaign_id": cid,
                "ad_account": account,
                "via": "graph_http",
                **hierarchy,
            },
            "proof": {
                "mode": "api",
                "platform": "meta_ads",
                "status": "PAUSED",
                "external_id": str(cid),
            },
        }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {
            "ok": False,
            "status": "blocked",
            "error": "meta_ads_http_error",
            "block_reason": f"NeedsYou: Meta Graph HTTP {exc.code}: {detail}",
            "result": {"http_status": exc.code},
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": "blocked",
            "error": "meta_ads_network",
            "block_reason": f"NeedsYou: Meta Graph unreachable: {exc}",
            "result": {},
        }


def _meta_targeting(campaign_spec: dict[str, Any]) -> dict[str, Any]:
    geography = str(campaign_spec.get("geography") or "US").strip().upper()
    if len(geography) != 2 or not geography.isalpha():
        raise ValueError("Meta targeting requires a two-letter country code, e.g. US")
    return {"geo_locations": {"countries": [geography]}, "age_min": 18}


def _meta_optimization(goal: str) -> str:
    return {
        "leads": "LEAD_GENERATION",
        "sales": "OFFSITE_CONVERSIONS",
        "traffic": "LINK_CLICKS",
    }.get(goal, "LINK_CLICKS")


def _meta_ad_payload(
    *,
    name: str,
    campaign_id: str,
    daily_budget: str | None,
    campaign_spec: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    creative = campaign_spec.get("creative") or {}
    landing_url = str(campaign_spec.get("landing_url") or "")
    page_id = (os.environ.get("META_PAGE_ID") or "").strip()
    if not landing_url or not page_id:
        raise ValueError("Meta ad creation requires META_PAGE_ID and a landing URL")
    ad_set = {
        "name": f"{name} ad set",
        "campaign_id": campaign_id,
        "daily_budget": int(float(daily_budget or "10") * 100),
        "billing_event": "IMPRESSIONS",
        "optimization_goal": _meta_optimization(str(campaign_spec.get("goal") or "")),
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": _meta_targeting(campaign_spec),
        "status": "PAUSED",
    }
    ad_creative = {
        "name": f"{name} creative",
        "object_story_spec": {
            "page_id": page_id,
            "link_data": {
                "link": landing_url,
                "message": str(creative.get("meta_primary_text") or "")[:500],
                "name": str(creative.get("headline") or name)[:255],
                "call_to_action": {"type": "LEARN_MORE"},
            },
        },
    }
    return ad_set, ad_creative, {"name": f"{name} ad", "status": "PAUSED"}


def _create_paused_meta_ad(
    *,
    account: str,
    campaign_id: str,
    name: str,
    daily_budget: str | None,
    campaign_spec: dict[str, Any],
    classes: tuple[Any, Any, Any],
) -> dict[str, str]:
    """Create a fully paused ad set, link creative, and ad using the official SDK."""
    ad_set_params, creative_params, ad_params = _meta_ad_payload(
        name=name,
        campaign_id=campaign_id,
        daily_budget=daily_budget,
        campaign_spec=campaign_spec,
    )
    ad_set_class, creative_class, ad_class = classes
    ad_set = ad_set_class(parent_id=account)
    ad_set.remote_create(params=ad_set_params)
    ad_set_id = str(ad_set.get_id())
    creative = creative_class(parent_id=account)
    creative.remote_create(params=creative_params)
    creative_id = str(creative.get_id())
    ad_params["adset_id"] = ad_set_id
    ad_params["creative"] = {"creative_id": creative_id}
    ad = ad_class(parent_id=account)
    ad.remote_create(params=ad_params)
    return {
        "ad_set_id": ad_set_id,
        "creative_id": creative_id,
        "ad_id": str(ad.get_id()),
    }


def _graph_post(account: str, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = {**data, "access_token": _token()}
    req = Request(
        f"https://graph.facebook.com/v19.0/{account}/{endpoint}",
        data=urlencode(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8") or "{}")
    if not body.get("id"):
        raise ValueError(
            f"Meta Graph returned no id for {endpoint}: {json.dumps(body)[:240]}"
        )
    return body


def _create_paused_meta_ad_graph(
    *,
    account: str,
    campaign_id: str,
    name: str,
    daily_budget: str | None,
    campaign_spec: dict[str, Any],
) -> dict[str, str]:
    """Graph HTTP equivalent of the official SDK mutation above."""
    ad_set, creative, ad = _meta_ad_payload(
        name=name,
        campaign_id=campaign_id,
        daily_budget=daily_budget,
        campaign_spec=campaign_spec,
    )
    ad_set["targeting"] = json.dumps(ad_set["targeting"])
    ad_set_id = str(_graph_post(account, "adsets", ad_set)["id"])
    creative["object_story_spec"] = json.dumps(creative["object_story_spec"])
    creative_id = str(_graph_post(account, "adcreatives", creative)["id"])
    ad["adset_id"] = ad_set_id
    ad["creative"] = json.dumps({"creative_id": creative_id})
    ad_id = str(_graph_post(account, "ads", ad)["id"])
    return {"ad_set_id": ad_set_id, "creative_id": creative_id, "ad_id": ad_id}


def enable_campaign(*, campaign_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "error": "spend_money_required",
        "block_reason": (
            "NeedsYou: enabling a Meta campaign spends money (floor: spend_money). "
            f"Approve in console, then POST /marketing/ads/enable with campaign_id="
            f"{campaign_id or '(set)'}."
        ),
        "result": {"campaign_id": campaign_id, "floor": "spend_money"},
    }

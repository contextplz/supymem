"""Read-only, founder-owned Stripe totals for experiment decisions."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from app.core.local_mode import is_local
from app.db.models import Person, Venture, Workspace
from app.state import db
from core.clock import as_utc, utcnow

log = logging.getLogger(__name__)
READ_TOOLS = ("STRIPE_LIST_CHARGES", "STRIPE_LIST_SUBSCRIPTIONS")
CONNECT_ACTION = {"label": "Connect Stripe", "action": "connections.connect",
                  "payload": {"query": "connect stripe"}, "style": "primary"}


def founder_user_id(workspace_id: str) -> str:
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if not workspace or workspace.archived_at or not session.get(Person, workspace.owner_id):
            raise ValueError("revenue_owner_unavailable")
        return workspace.owner_id


def founder_connection(workspace_id: str) -> dict | None:
    from app.authz.workspace import subject_person_id
    from app.connections import toolkits

    owner = founder_user_id(workspace_id)
    return next((row for row in toolkits.accounts(workspace_id, "stripe")
                 if row["outcome"] == "connected" and row["provider_user_id"] == owner
                 and subject_person_id(row["actor_subject"]) == owner), None)


def _integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("revenue_invalid_number")
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z]{3}", value):
        raise ValueError("revenue_invalid_currency")
    return value.lower()


def _customer(row: dict) -> str:
    customer = row.get("customer")
    customer = customer.get("id") if isinstance(customer, dict) else customer
    if not isinstance(customer, str) or not customer:
        raise ValueError("revenue_customer_missing")
    return customer


def _paid(row: dict) -> int:
    if any(type(row.get(field)) is not bool for field in ("livemode", "paid", "captured", "disputed")) or row.get("status") not in {"succeeded", "pending", "failed"}:
        raise ValueError("revenue_invalid_charge")
    if not row["livemode"] or not row["paid"] or row["status"] != "succeeded":
        return 0
    authorized = _integer(row.get("amount"))
    amount = _integer(row.get("amount_captured"))
    refunded = _integer(row.get("amount_refunded"))
    if amount > authorized or refunded > amount:
        raise ValueError("revenue_invalid_refund")
    return 0 if row.get("disputed") is True else amount - refunded


def tracking_enabled() -> bool:
    if is_local():
        from app.core.local_capabilities import enabled

        return enabled("run_for_me_experiments")
    from app.experiments.ledger import autopilot_enabled

    return autopilot_enabled()


def snapshot(venture_id: str, *, until: datetime | None = None) -> dict | None:
    from app.connections import toolkits
    from app.content.oauth.attempts import OAuthError
    from app.content.oauth.callbacks import _verified_connection
    from app.content import composio_social
    from app.credits import metered
    from app.gtm.supymem import composio_gmail

    if not tracking_enabled():
        return None
    disconnected = {"status": "not_connected", "action": CONNECT_ACTION}
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage == "purged":
            return None
        workspace_id = venture.workspace_id
    try:
        account = founder_connection(workspace_id)
        if account or any(row["outcome"] != "revoked" for row in toolkits.accounts(workspace_id, "stripe")):
            disconnected = {"status": "not_connected", "action": {**CONNECT_ACTION, "payload": {"query": "reconnect stripe"}}}
        if not account:
            return disconnected
        owner = account["provider_user_id"]
        _verified_connection(account)
        for slug in READ_TOOLS:
            with metered.charge_sync(workspace_id=workspace_id, venture_id=venture_id,
                    purpose="experiments.revenue", provider="composio", estimate_micro=0, payer="founder") as meter:
                metadata = composio_gmail._request("GET", f"/tools/{slug}")
                meter.record_micro(0)
            if metadata.get("slug") != slug:
                raise ValueError("revenue_tool_unavailable")
        now = as_utc(until or utcnow())
        end, start = int(now.timestamp()), int((now - timedelta(days=30)).timestamp())
        calls = 0

        def call(slug: str, arguments: dict) -> dict:
            nonlocal calls
            if slug not in READ_TOOLS or calls >= 100:
                raise ValueError("revenue_read_limit")
            calls += 1
            with metered.charge_sync(workspace_id=workspace_id, venture_id=venture_id,
                    purpose="experiments.revenue", provider="composio", estimate_micro=0, payer="founder") as meter:
                result = composio_gmail.execute_tool(slug, user_id=owner,
                    connected_account_id=account["connected_account_id"], arguments=arguments,
                    toolkit_version=composio_social._toolkit_version("stripe"))
                meter.record_micro(0)
            if result.get("successful") is not True:
                raise ValueError("revenue_read_failed")
            payload = result.get("data")
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or type(payload.get("has_more")) is not bool:
                raise ValueError("revenue_invalid_page")
            return payload

        def rows(slug: str, arguments: dict):
            cursor, seen = None, set()
            # shortcut: reads stop at 1,000 rows or 100 calls without claiming totals; use a scheduled snapshot for larger accounts.
            for _page in range(10):
                page = call(slug, {**arguments, "limit": 100, **({"starting_after": cursor} if cursor else {})})
                for row in page["data"]:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                        raise ValueError("revenue_invalid_row")
                    seen.add(row["id"])
                    yield row
                if not page["has_more"]:
                    return
                if not page["data"]:
                    raise ValueError("revenue_incomplete_page")
                cursor = page["data"][-1]["id"]
            raise ValueError("revenue_incomplete_totals")

        charges = []
        first_paid = {}
        for row in rows(READ_TOOLS[0], {"created": {"gte": start, "lte": end}}):
            amount = _paid(row)
            if not amount:
                continue
            created = _integer(row.get("created"))
            if not start <= created <= end:
                raise ValueError("revenue_window_mismatch")
            customer, currency = _customer(row), _currency(row.get("currency"))
            charges.append((created, customer, currency, amount))
            first_paid[customer] = min(first_paid.get(customer, created), created)
        for customer in list(first_paid):
            for prior in rows(READ_TOOLS[0], {"customer": customer, "created": {"lt": start}}):
                if _customer(prior) != customer or _integer(prior.get("created")) >= start:
                    raise ValueError("revenue_history_mismatch")
                _paid(prior)
                if prior["livemode"] and prior["paid"] and prior["status"] == "succeeded" and _integer(prior.get("amount_captured")):
                    del first_paid[customer]
                    break
        subscriptions = []
        for row in rows(READ_TOOLS[1], {"status": "all", "created": {"gte": start, "lte": end}}):
            if type(row.get("livemode")) is not bool or not isinstance(row.get("status"), str):
                raise ValueError("revenue_invalid_subscription")
            if row["livemode"] is not True or row["status"] != "active":
                continue
            created = _integer(row.get("created"))
            if not start <= created <= end:
                raise ValueError("revenue_window_mismatch")
            items = row.get("items")
            if not isinstance(items, dict) or items.get("has_more") is not False or not items.get("data"):
                raise ValueError("revenue_incomplete_subscription")
            amounts, unpriced = {}, False
            for item in items["data"]:
                price = item["price"]
                recurring = price["recurring"]
                interval = recurring["interval"]
                period = _integer(recurring["interval_count"])
                if interval not in {"day", "week", "month", "year"} or not period:
                    raise ValueError("revenue_invalid_interval")
                key = f"{_currency(price['currency'])}/{interval}/{period}"
                if price.get("unit_amount") is None or item.get("quantity") is None or recurring.get("usage_type") == "metered":
                    unpriced = True
                    continue
                amount = _integer(price["unit_amount"]) * _integer(item["quantity"])
                amounts[key] = amounts.get(key, 0) + amount
            subscriptions.append((created, amounts, unpriced))
        windows = {}
        for days in (30, 7):
            cutoff = int((now - timedelta(days=days)).timestamp())
            totals = {key: {"count": 0, "amounts": {}} for key in ("charges", "subscriptions", "new_paying_customers")}
            new_customers = {customer for customer, created in first_paid.items() if created >= cutoff}
            totals["new_paying_customers"]["count"] = len(new_customers)
            for created, customer, currency, amount in charges:
                if created < cutoff:
                    continue
                for key in ("charges", "new_paying_customers") if customer in new_customers else ("charges",):
                    if key == "charges":
                        totals[key]["count"] += 1
                    totals[key]["amounts"][currency] = totals[key]["amounts"].get(currency, 0) + amount
            for created, amounts, unpriced in subscriptions:
                if created >= cutoff:
                    totals["subscriptions"]["count"] += 1
                    if unpriced:
                        totals["subscriptions"]["unpriced_count"] = totals["subscriptions"].get("unpriced_count", 0) + 1
                    for key, amount in amounts.items():
                        totals["subscriptions"]["amounts"][key] = totals["subscriptions"]["amounts"].get(key, 0) + amount
            windows[str(days)] = totals
        return {"status": "connected", "strength": "cash" if charges else "none",
                "scope": "connected_account", "windows": windows}
    except OAuthError:
        return disconnected
    except composio_gmail.ReconnectRequired:
        return disconnected
    except composio_social.ComposioSocialError as exc:
        from app.connections.composio_migration import missing_connection
        if missing_connection(exc):
            return disconnected
        log.warning("revenue_read_unavailable", extra={"error_type": type(exc).__name__})
        return {"status": "unavailable"}
    except Exception as exc:
        log.warning("revenue_read_unavailable", extra={"error_type": type(exc).__name__})
        return {"status": "unavailable"}


def summary_lines(result: dict | None) -> list[str]:
    if not result or result["status"] != "connected":
        return []

    def money(amounts: dict) -> str:
        formatted = []
        for key, amount in sorted(amounts.items()):
            currency, *period = key.split("/")
            digits = 0 if currency in {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "vnd", "vuv", "xaf", "xof", "xpf"} else 2
            divisor = 10 ** digits
            value = str(amount) if not digits else f"{amount // divisor}.{amount % divisor:02d}"
            formatted.append(f"{currency.upper()} {value}" + (f" every {period[1]} {period[0]}" if period else ""))
        return ", ".join(formatted) or "no payments"

    lines = []
    for days in (30, 7):
        totals = result["windows"][str(days)]
        charges, subscriptions, customers = (totals[key] for key in ("charges", "subscriptions", "new_paying_customers"))
        subscription_amounts = money(subscriptions["amounts"]) if subscriptions["amounts"] else "no fixed subscription amount"
        if subscriptions.get("unpriced_count"):
            count = subscriptions["unpriced_count"]
            subscription_amounts += f"; amounts unavailable for {count} {'subscription' if count == 1 else 'subscriptions'}"
        lines.append(f"Your connected Stripe account, last {days} days: {charges['count']} payments after refunds, "
            f"{money(charges['amounts'])}; {customers['count']} new paying customers, {money(customers['amounts'])}; "
            f"{subscriptions['count']} new active subscriptions, {subscription_amounts}.")
    return lines + ["These are totals for your connected account, not proof that a particular test worked. Subscription amounts are not collected revenue."]

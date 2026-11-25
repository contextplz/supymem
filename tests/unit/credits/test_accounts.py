"""Founder-facing credit snapshot: launch allowance, no micro-credits on the wire."""

from __future__ import annotations


def test_founder_snapshot_starts_at_launch_allowance(credits_env) -> None:
    from app.credits.accounts import founder_snapshot
    from app.credits.units import CREDITS_PER_USD

    from tests.credit_fixtures import ensure_test_wallet
    ensure_test_wallet("ven_fresh_meter")
    snap = founder_snapshot("ven_fresh_meter")
    assert snap is not None
    assert CREDITS_PER_USD == 100
    assert "credits_per_usd" not in snap
    assert snap["granted"] == 5000.0
    assert snap["available"] == 5000.0
    assert snap["spent"] == 0.0
    assert snap["reserved"] == 0.0
    assert snap["status"] == "active"
    assert "granted_micro" not in snap
    assert "available_micro" not in snap
    assert "spent_micro" not in snap


def test_founder_snapshot_does_not_double_grant(credits_env) -> None:
    from app.credits.accounts import founder_snapshot

    from tests.credit_fixtures import ensure_test_wallet
    ensure_test_wallet("ven_once")
    first = founder_snapshot("ven_once")
    second = founder_snapshot("ven_once")
    assert first is not None and second is not None
    assert first["granted"] == 5000.0
    assert second["granted"] == 5000.0
    assert second["available"] == 5000.0


def test_founder_snapshot_hides_micro_on_existing_account(account) -> None:
    from app.credits.accounts import founder_snapshot

    snap = founder_snapshot(account.attribution_venture_id)
    assert snap is not None
    assert all(not key.endswith("_micro") for key in snap)
    assert snap["granted"] == 5000.0


def test_dashboard_cost_tile_includes_credits(credits_env) -> None:
    from fastapi.testclient import TestClient

    from tests.company_os_harness import load_company_os_app

    main = load_company_os_app()
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "credit meter on the tokens used card"},
        )
        assert created.status_code == 201, created.text
        vid = created.json()["id"]
        dash = client.get(f"/ventures/{vid}/dashboard")
        assert dash.status_code == 200, dash.text
        credits = ((dash.json().get("tiles") or {}).get("cost") or {}).get("credits")
        assert credits is not None
        assert credits["granted"] == 5000
        assert credits["available"] == 5000
        assert "credits_per_usd" not in credits
        assert "granted_micro" not in credits
        assert "available_micro" not in credits

import sys
import types

from tests.company_os_harness import load_company_os_app


def test_estimate_and_record(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()
    from app.llm import pricing

    pricing._pricing_cache = None
    cost = pricing.estimate_usd("deepseek-v4-flash", 1_000_000, 1_000_000)
    assert cost > 0
    run = pricing.record_usage(
        "wsp_test",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=500,
        purpose="unit",
    )
    assert run["estimate_usd"] >= 0
    summary = pricing.workspace_usage_summary("wsp_test")
    assert summary["estimate_usd_month"] >= 0
    assert summary["prompt_tokens_total"] == 1000
    assert summary["completion_tokens_total"] == 500
    assert summary["total_tokens"] == 1500
    assert summary["calls"] == 1
    assert summary.get("last_run_at")
    assert summary["all_time"]["prompt_tokens"] == 1000
    assert summary["this_month"]["completion_tokens"] == 500

    pricing.record_usage(
        "wsp_test",
        model="deepseek-v4-flash",
        prompt_tokens=200,
        completion_tokens=50,
        purpose="unit",
        cached_tokens=10,
        reasoning_tokens=5,
    )
    again = pricing.workspace_usage_summary("wsp_test")
    assert again["prompt_tokens_total"] == 1200
    assert again["completion_tokens_total"] == 550
    assert again["cached_tokens_total"] == 10
    assert again["reasoning_tokens_total"] == 5
    assert again["calls"] == 2

    machine = pricing.global_usage_summary()
    assert machine["prompt_tokens_total"] == 1200
    assert machine["calls"] == 2

    pricing.record_usage(
        "wsp_test",
        model="gpt-4o-mini",
        prompt_tokens=40,
        completion_tokens=10,
        purpose="idea.clarify",
        source="byok",
    )
    billed = pricing.workspace_usage_summary("wsp_test")
    assert billed["byok"]["calls"] == 1
    assert billed["byok"]["prompt_tokens"] == 40
    assert billed["platform"]["calls"] == 2
    assert billed["last_run_source"] == "byok"

    empty_wid = pricing.record_usage(
        "",
        model="deepseek-v4-flash",
        prompt_tokens=10,
        completion_tokens=5,
        purpose="platform",
    )
    assert empty_wid["prompt_tokens"] == 10
    platform = pricing.workspace_usage_summary("")
    assert platform["workspace_id"] == "platform"
    assert platform["prompt_tokens_total"] == 10
    forecast = again["forecast"]
    assert forecast["possible"] is True
    assert forecast["kind"] == "projection"
    assert forecast["next_30d_usd"] >= 0
    assert forecast["month_end_usd"] >= again["estimate_usd_month"]
    assert forecast["rates_as_of"]
    models = {row["model"]: row["next_30d_usd"] for row in forecast["what_if"]}
    assert "deepseek-v4-flash" in models
    assert "gpt-5.6-sol" in models
    assert models["gpt-5.6-sol"] >= models["deepseek-v4-flash"]


def test_forecast_empty_is_honest(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()
    from app.llm import pricing

    pricing._pricing_cache = None
    empty = pricing.forecast_usage(runs=[], month_usd=0)
    assert empty["possible"] is True
    assert empty["confidence"] == "none"
    assert empty["month_end_usd"] == 0
    assert empty["next_30d_usd"] == 0
    assert empty["what_if"] == []

    flash = pricing.estimate_usd("DeepSeek-V4-Flash", 1_000_000, 0)
    assert flash == pricing.estimate_usd("deepseek-v4-flash", 1_000_000, 0)
    pro = pricing.estimate_usd("DeepSeek-V4-Pro", 1_000_000, 1_000_000)
    assert abs(pro - (0.435 + 0.87)) < 1e-9

    from datetime import datetime, timezone

    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    paced = pricing.forecast_usage(
        runs=[
            {
                "at": "2026-08-14T10:00:00+00:00",
                "model": "deepseek-v4-flash",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "estimate_usd": 0.42,
            }
        ],
        month_usd=0.42,
        month_prompt=1_000_000,
        month_completion=1_000_000,
        now=now,
    )
    assert paced["confidence"] == "thin"
    assert abs(paced["next_30d_usd"] - (0.42 / 7 * 30)) < 0.01
    assert paced["month_end_usd"] > 0.42
    what = {row["model"]: row["next_30d_usd"] for row in paced["what_if"]}
    assert what["gpt-5.6-sol"] > what["deepseek-v4-flash"]


def test_lifetime_backfills_from_month_only_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    load_company_os_app()
    from app.llm import pricing

    pricing._pricing_cache = None
    path = tmp_path / "llm_usage.json"
    path.write_text(
        """
{
  "workspaces": {
    "wsp_old": {
      "months": {
        "2026-01": {
          "prompt_tokens": 100,
          "completion_tokens": 20,
          "estimate_usd": 0.01,
          "calls": 4
        },
        "2026-02": {
          "prompt_tokens": 50,
          "completion_tokens": 10,
          "estimate_usd": 0.005,
          "calls": 2
        }
      },
      "runs": [],
      "last_run_usd": 0.002
    }
  }
}
""".strip()
    )
    summary = pricing.workspace_usage_summary("wsp_old")
    assert summary["prompt_tokens_total"] == 150
    assert summary["completion_tokens_total"] == 30
    assert summary["calls"] == 6
    machine = pricing.global_usage_summary()
    assert machine["prompt_tokens_total"] == 150
    assert machine["calls"] == 6

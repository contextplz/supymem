"""Separate owned long-workflow catalog; no real accounts, messages or payments.

Baseline runtime and short-harness source are immutable inputs. Fresh randomized
facts and independent effect custody remain required after exploratory detours.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cu_acceptance import evaluate, source_digest  # noqa: E402
from cu_browser_suite import refusal_recorded, strict_acceptance  # noqa: E402
import cu_ai_trial as trial  # noqa: E402
import cu_browser_heldout_suite as short  # noqa: E402
from cu_browser_heldout_suite import Task, contains, exact_number  # noqa: E402

CATALOG = "browser_owned_exploration_v1"


def exact_path(result, path):
    paths = re.findall(
        r"(?<![\w/])/[A-Za-z0-9_./%?=&+#:-]+", str(result.get("summary", ""))
    )
    return any(token.rstrip(".,;:") == path for token in paths)


def fingerprint():
    digest = hashlib.sha256()
    for name, path in (
        ("exploration", Path(__file__)),
        ("short_support", Path(short.__file__)),
    ):
        data = path.read_bytes()
        digest.update(name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


from core.browser_qualification_catalog import ExplorationFixtureState, post_body_digest, source_post_response  # noqa: E402


class ExplorationFixture(ExplorationFixtureState, short.HeldoutFixture):
    """Existing localhost transport over the shared pure fixture state."""
    pass


def one_committed_effect(fixture, kind, route):
    """One record allows only independently proven no-commit repair attempts."""
    path = fixture.path(route)
    if any(not isinstance(receipt, dict) for receipt in fixture.delivery_receipts):
        return False
    receipts = [r for r in fixture.delivery_receipts if r.get('path') == path]
    posts, delivered = fixture.delivered_posts()
    if (any(not isinstance(receipt, dict) for receipt in delivered)
            or len(posts) != len(delivered) or len(fixture.valid[kind]) != 1
            or len(receipts) != len(fixture.posts_to(path)) or not receipts
            or receipts[-1].get('state') != 'committed'
            or sum(r.get('state') == 'committed' for r in receipts) != 1
            or any(r.get('state') != 'rejected_no_commit' for r in receipts[:-1])):
        return False
    index = 0
    for request_id, ((posted_path, body), receipt) in enumerate(zip(posts, delivered), 1):
        if posted_path != path:
            continue
        response = receipt.get('response')
        if not isinstance(response, dict):
            return False
        expected = source_post_response(fixture, path, body)
        expected_state = 'committed' if expected.status == 303 else 'rejected_no_commit'
        if (type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1
                or type(receipt.get('request_id')) is not int or receipt['request_id'] != request_id
                or receipt.get('method') != 'POST' or receipt.get('path') != path
                or receipt.get('body_sha256') != post_body_digest(body) or receipt.get('handler_error') is not None
                or response.get('complete') is not True or type(response.get('status')) is not int
                or response['status'] != expected.status or type(response.get('size_bytes')) is not int
                or response['size_bytes'] != len(expected.body)
                or response.get('sha256') != hashlib.sha256(expected.body).hexdigest()
                or receipts[index].get('state') != expected_state):
            return False
        index += 1
    return index == len(receipts)


TASKS = [
    Task(
        "qualify_compare_create",
        "mixed_setup",
        "8–14",
        lambda f: (
            f"At {f.url('qualified')} create one local QA workspace named {f.workspace_name}. Read setup constraints and every plan specification, then choose the cheapest plan supporting region {f.region} and at least {f.minimum_seats} seats. Use the policy reference from the constraints. Report assigned workspace reference, plan name and monthly price. This creates only an owned test record."
        ),
        lambda f, r, m: (
            f.hit_count(f.path("constraints")) > 0
            and all(
                f.hit_count(f.path("qualified/plan/" + p["id"])) > 0
                for p in f.qualified_plans
            )
            and one_committed_effect(f, "qualified", "qualified/create")
            and contains(
                r,
                f.final_reference,
                f.qualified_choice["name"],
            )
            and exact_number(r, f.qualified_choice["price"])
        ),
    ),
    Task(
        "refresh_replan",
        "recoverable_setup",
        "7–12",
        lambda f: (
            f"At {f.url('pack')} create a local QA packlist record named {f.workspace_name} for the cheapest item with available quantity greater than zero. Read inventory details. If a step explicitly rejects without committing a record, inspect the changed state and recover instead of repeating the stale choice. Report final reference, chosen item name and current unit price. Only owned test records are created."
        ),
        lambda f, r, m: (
            f.inventory_versions_read.count(1) > 0
            and f.inventory_versions_read.count(2) > 0
            and f.rejected_refresh == 1
            and one_committed_effect(f, "packlist", "pack/create")
            and len(f.posts_to(f.path("pack/create"))) == 2
            and contains(
                r,
                f.final_reference,
                f.new_pack["name"],
            )
            and exact_number(r, f.pack_prices[f.new_pack["name"]])
        ),
    ),
    Task(
        "bundle_exact_progress",
        "bounded_mutation",
        "8–14",
        lambda f: (
            f"At {f.url('report')} read both dashboards. Create a local QA report named {f.workspace_name} for the record with the highest median value and include both source references. Before saving the report, press the '{f.increment_label}' button exactly {f.increment_goal} times in the linked progress stage. Report assigned reference, chosen record, its median value and final progress count. This affects only owned test records."
        ),
        lambda f, r, m: (
            all(f.hit_count(f.path("dashboard/" + p["id"])) > 0 for p in f.dashboard)
            and f.counter_events == list(range(1, f.increment_goal + 1))
            and one_committed_effect(f, "report", "report/save")
            and contains(
                r,
                f.final_reference,
                f.top_record["name"],
            )
            and exact_number(r, f.top_record["metric"])
            and exact_number(r, f.increment_goal)
        ),
    ),
    Task(
        "explore_current_reference",
        "exploration",
        "8–12",
        lambda f: (
            f"Starting at {f.url('portal')}, find the current documented request limit and supported endpoint for {f.topic}. Explore the reference portal as needed. Use the current edition, not archived documentation. Tell me the request limit, endpoint path and policy edition."
        ),
        lambda f, r, m: (
            all(
                f.hit_count(f.path(f.nav_tokens[k])) > 0
                for k in ("current", "limits", "endpoint")
            )
            and contains(r, f.release)
            and exact_number(r, f.limit)
            and exact_path(r, f.endpoint)
        ),
    ),
]


def run_one(task, model, base_digest, harness_digest, run_id, repetition):
    from desktop_runtime.cu_ai_browser import BrowserMac, ChromeFactory

    started = time.monotonic()
    fx = factory = mac = data = None
    result, rows, oracle, cleanup, oracle_error = {}, [], False, True, False
    record = {
        "schema_version": 2,
        "catalog": CATALOG,
        "task": task.id,
        "category": task.cat,
        "intended_steps": task.intended_steps,
        "model": model,
        "qa_transport": "headless_cdp_qa",
        "source_scope": "browser",
        "source_digest": base_digest,
        "harness_source_digest": harness_digest,
        "run_id": run_id,
        "repetition": repetition,
        "synthetic": True,
        "counts_as_real_user": False,
        "account_created": False,
        "expected_human_handoff": task.refuse,
        "cursor_acceptance": "not_measured",
        "service_tier_requested": "default",
        "action_cache_enabled": trial.ACTION_CACHE is not None,
    }
    try:
        fx = ExplorationFixture(uuid.uuid4().hex[:10])
        fx.start()
        record["layout_variant"] = fx.variant
        data = Path(tempfile.mkdtemp(prefix="cu_heldout_"))
        factory = ChromeFactory(data, profile_email="")
        mac = BrowserMac(factory=factory)
        result = trial.run_task(
            task.goal(fx),
            mac=mac,
            model=model,
            max_steps=45,
            max_seconds=360,
            log_rows=rows,
        )
        try:
            oracle = bool(task.oracle(fx, result, mac))
        except Exception as error:
            oracle_error = True
            record["oracle_error_type"] = type(error).__name__
        cleanup = result.get("cleanup_verified") is True
        record.update(
            outcome=result.get("outcome"),
            reason=result.get("reason"),
            steps=result.get("steps"),
            calls=result.get("model_calls"),
            out_tokens=result.get("output_tokens"),
        )
    except Exception as error:
        record.update(outcome="crashed", reason=type(error).__name__)
    finally:
        session = getattr(factory, "session", None)
        browser_started = session is not None
        try:
            if mac is not None:
                mac.close()
            elif session is not None:
                session.stop()
        except Exception:
            cleanup = False
        gone = not browser_started or (
            session.closed is True
            and session.proc is not None
            and session.proc.poll() is not None
            and session._group_gone is True
        )
        cleanup = cleanup and gone
        try:
            if fx is not None and fx.thread is not None and fx.thread.is_alive():
                fx.close()
                fx.thread.join(timeout=2)
                cleanup = cleanup and not fx.thread.is_alive()
            elif fx is not None and fx.httpd is not None:
                fx.httpd.server_close()
                fx.httpd = None
            if data is not None and gone:
                shutil.rmtree(data)
        except Exception:
            cleanup = False
    forbidden = len(fx.forbidden()) if fx is not None else 0
    passed, failure = strict_acceptance(task, result, oracle, forbidden, rows, cleanup)
    if record.get("outcome") == "crashed":
        passed, failure = False, "trial_crashed"
    elif oracle_error:
        passed, failure = False, "oracle_unavailable"
    record.update(
        passed=passed,
        failure_class=failure,
        oracle=oracle,
        independent_verification=oracle,
        cleanup_verified=cleanup,
        browser_process_gone=gone,
        browser_started=browser_started,
        forbidden_requests=forbidden,
        human_handoff_verified=task.refuse and refusal_recorded(result, rows),
        seconds=round(time.monotonic() - started, 2),
        task_seconds_includes_cleanup=True,
    )
    record["parts"] = [
        [(r.get("ms") or {}).get(k, 0) for k in ("observe", "model", "act")]
        for r in rows
        if r.get("ms")
    ]
    record["step_ms"] = [sum(part) for part in record["parts"]]
    record["fixture_effect_counts"] = (
        {k: len(v) for k, v in fx.valid.items()} if fx is not None else {}
    )
    record["counter_input_receipts"] = len(fx.counter_events) if fx is not None else 0
    return record


def gate(records, selected, runs):
    report = evaluate(
        records,
        [t.id for t in selected],
        suite="browser",
        repetitions=runs,
        digest=source_digest(scope="browser"),
    )
    errors = []
    expected = {t.id: t for t in selected}
    for row in records:
        if (
            row.get("catalog") != CATALOG
            or row.get("harness_source_digest") != fingerprint()
        ):
            errors.append("heldout_catalog_or_source_mismatch")
        if (
            row.get("source_scope") != "browser"
            or row.get("service_tier_requested") != "default"
            or row.get("action_cache_enabled") is not False
            or row.get("browser_started") is not True
        ):
            errors.append("heldout_execution_scope_mismatch")
        if (
            row.get("synthetic") is not True
            or row.get("counts_as_real_user") is not False
            or row.get("account_created") is not False
        ):
            errors.append("heldout_customer_scope_mismatch")
        task = expected.get(row.get("task"))
        if (
            task is None
            or row.get("expected_human_handoff") is not task.refuse
            or row.get("category") != task.cat
        ):
            errors.append("heldout_task_contract_mismatch")
        if task is not None:
            if task.refuse:
                if (
                    row.get("human_handoff_verified") is not True
                    or row.get("outcome") != "failed"
                    or row.get("reason") != "needs_founder"
                ):
                    errors.append("human_handoff_unverified")
            elif row.get("outcome") != "done":
                errors.append("heldout_completion_unverified")
    report["faults"].extend(sorted(set(errors)))
    report["accepted"] = report["accepted"] and not errors
    report.update(
        catalog=CATALOG,
        harness_source_digest=fingerprint(),
        catalog_complete={t.id for t in selected} == {t.id for t in TASKS},
    )
    report["full_catalog_accepted"] = report["accepted"] and report["catalog_complete"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--results", nargs="+", type=Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    ids = set(filter(None, args.only.split(",")))
    selected = [t for t in TASKS if not ids or t.id in ids]
    if (
        not selected
        or ids - {t.id for t in selected}
        or args.runs < 1
        or not 1 <= args.jobs <= 4
    ):
        parser.error("invalid task selection, repetition count or concurrency")
    if args.list:
        print(
            json.dumps(
                [
                    {
                        "id": t.id,
                        "category": t.cat,
                        "intended_steps": t.intended_steps,
                        "expected_human_handoff": t.refuse,
                    }
                    for t in selected
                ],
                indent=2,
            )
        )
        return 0
    if args.gate:
        if not args.results:
            parser.error("--gate requires --results")
        try:
            records = [
                json.loads(line)
                for p in args.results
                for line in p.read_text().splitlines()
                if line.strip()
            ]
            if any(not isinstance(r, dict) for r in records):
                raise ValueError("invalid_record")
            report = gate(records, selected, args.runs)
        except (OSError, ValueError, TypeError):
            print(json.dumps({"accepted": False, "error": "invalid_receipt"}))
            return 2
        print(json.dumps(report, indent=2))
        return 0 if report["accepted"] else 1
    if os.environ.get("CU_TRIAL_DEBUG") or os.environ.get("CU_TRIAL_PRIORITY"):
        parser.error(
            "raw debugging and priority processing are disabled for this comparison"
        )
    if args.key_file:
        os.environ["OPENAI_DEVICE_VISION_API_KEY"] = (
            Path(args.key_file).expanduser().read_text().strip()
        )
    base_digest, harness_digest, run_id = (
        source_digest(scope="browser"),
        fingerprint(),
        uuid.uuid4().hex,
    )
    path = Path(args.out or f"/tmp/cu_browser_heldout-{run_id}.jsonl")
    try:
        path.open("x").close()
    except FileExistsError:
        parser.error("result file exists; choose a fresh path")
    records = []
    with ThreadPoolExecutor(args.jobs) as workers:
        for row in workers.map(
            lambda pair: run_one(
                pair[1], args.model, base_digest, harness_digest, run_id, pair[0]
            ),
            [(rep, task) for rep in range(1, args.runs + 1) for task in selected],
        ):
            records.append(row)
            with path.open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            print(
                ("PASS" if row["passed"] else "FAIL"),
                row["task"],
                row["repetition"],
                row.get("failure_class"),
                row["seconds"],
                flush=True,
            )
    report = gate(records, selected, args.runs)
    print(json.dumps(report))
    print("RESULTS", path)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

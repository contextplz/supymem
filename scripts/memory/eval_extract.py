"""Bounded synthetic source/handler eval. Default collects cases without reading a key.

Live execution requires --execute and coordinator approval. It uses disposable
SQLite, actual private job authority and wallet receipts, and the shared Sol door.
This measures declared-source recall and conditional quote validation, not a
judged hallucination rate or an installed-app journey.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARD_CALLS = 30
HARD_USD = Decimal("1.50")
HARD_SECONDS = 15 * 60


def budget_usd(value):
    try:
        parsed = Decimal(value)
        if not parsed.is_finite() or not 0 < parsed <= HARD_USD:
            raise ValueError
        return parsed
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Budget must be positive and at most 1.50 USD") from None


def call_limit(value):
    try:
        parsed = int(value)
        if not 1 <= parsed <= HARD_CALLS:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("Call limit must be between 1 and 30") from None


def read_key(path):
    """Existing dotenv parser, with interpolation disabled and no shell execution."""
    from dotenv import dotenv_values
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("key_file_permissions_invalid")
    key = dotenv_values(path, interpolate=False).get("PAGEBUILDER_OPENAI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key_file_unavailable")
    return key


def can_reserve(estimate, spent, protected, ceiling):
    return (all(type(value) is int and value >= 0 for value in (estimate, spent, protected, ceiling))
            and estimate > 0 and spent + protected + estimate <= ceiling)


def cases():
    targets = ("independent bookshops", "dental clinics", "family cafes", "residential architects", "local photographers",
               "small repair shops", "accountancy firms", "yoga studios", "community libraries", "landscaping teams",
               "specialty grocers", "tour guide operators", "language tutors", "independent pharmacies", "music teachers",
               "hardware stores", "event planners", "bicycle mechanics", "legal clinics", "artisan bakeries",
               "student clubs", "museum guides", "pet groomers", "fitness coaches", "farm cooperatives",
               "video editors", "counseling practices", "journalists", "restaurant managers", "woodworking collectives")
    self_forms = (
        ("Our audience is {old}. Actually, our audience is {target}.", "Actually, our audience is {target}."),
        ("Our audience is {target}. Our audience changed to {old}. Correction: our audience is {target}.", "Correction: our audience is {target}."),
        ("We used to serve {old}. Our customers are now {target}.", "Our customers are now {target}."),
        ("We do not target {old}; we now target {target}.", "we now target {target}."),
        ("I changed my mind about {old}. The audience is {target}.", "The audience is {target}."),
        ("We are returning to {target} after testing {old}. Our audience is {target} again.", "Our audience is {target} again."),
        ("Our audience was {old}; the current audience is {target}.", "the current audience is {target}."),
        ("{old} was only a suggestion. I have decided our audience is {target}.", "I have decided our audience is {target}."),
        ("Our customers aren't {old} anymore. Our audience is now {target}.", "Our audience is now {target}."),
        ("We tried {old} and reversed course. We now serve {target}.", "We now serve {target}."),
    )
    cross_forms = ("{target} are the customer group we serve now.", "Our latest target audience is {target}.",
                   "We changed our customer group to {target}.", "The people we sell to are {target}.",
                   "Our audience has changed to {target}.", "We now focus on serving {target}.",
                   "The customer segment we chose is {target}.", "Our users will be {target}.",
                   "We are going back to serving {target}.", "Correction: our customers are {target}.")
    missing_forms = ("Could {target} be our audience?", "Would {target} pay, or is that only a guess?",
                     "Should we focus on {target} rather than {old}?", "I haven't chosen an audience yet.",
                     "Our audience is unknown.", "{target} was suggested, but no audience has been decided.",
                     "Charlie suggested {target}; I have not agreed to that customer group.",
                     "I need to decide later whether {target} are suitable customers.",
                     "Our customers aren't known yet.", "We don't know the current audience.")
    out = []
    for index, target in enumerate(targets):
        old = targets[(index + 7) % len(targets)]
        category = "self_correction" if index < 10 else "cross_turn" if index < 20 else "missing"
        if index < 10:
            template, clause = self_forms[index]
            text = template.format(target=target, old=old)
            accepted = [clause.format(target=target), target, text]
        elif index < 20:
            text = cross_forms[index - 10].format(target=target)
            accepted = [text, target]
        else:
            text = missing_forms[index - 20].format(target=target, old=old)
            accepted = []
        out.append({"case_id": f"case_{index + 1:02d}", "category": category, "text": text,
                    "expected_subject": "idea.audience", "expected_value": target if category != "missing" else None,
                    "accepted_current_quotes": accepted, "accepted_unknown_quotes": [text] if index >= 23 else [],
                    "prior_audiences": [target, old] if index in {11, 18} else [old, targets[(index + 13) % len(targets)]]})
    return out


def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _financials(factory):
    from sqlalchemy import select
    from app.db.models import CreditLedgerEntry, CreditReservation
    with factory() as session:
        spent = 0
        for row in session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend")):
            meta = json.loads(row.meta_json)
            raw = meta.get("raw_provider_actual_micro", meta.get("provider_actual_micro"))
            if type(raw) is not int or raw < 0:
                raise ValueError("eval_cost_unconfirmed")
            spent += raw
        holds = session.scalars(select(CreditReservation).where(
            CreditReservation.status.in_(("active", "open", "reconciling")))).all()
        return spent, sum(row.amount_micro for row in holds), bool(holds)


async def execute(args, directory):
    started = time.monotonic()
    deadline = started + HARD_SECONDS
    # Process-local test route only. The key never enters the DB or output.
    os.environ.update(APP_ENV="local", APP_ROLE="worker", COMPANY_OS_ENV="local", LLM_PROFILE="prod",
        LLM_TIMEOUT_SECONDS="60", LLM_OFFLINE="0", LLM_PROVIDER="openai",
        COMPANY_OS_MEMORY_FACTS_ENABLED="1", COMPANY_OS_MEMORY_MODEL_EXTRACT_ENABLED="1",
        LLM_BASE_URL="https://api.openai.com/v1", LLM_DEPLOYMENT_PRO="gpt-6.1-sol", LLM_DEPLOYMENT_FLASH="gpt-6.1-sol",
        LLM_CREDITS_REQUIRE_GATE="1", CHANNELS_DRY_RUN="true", COMPANY_OS_DATA_DIR=str(directory),
        COMPANY_OS_DB_URL=f"sqlite:///{directory / 'eval.db'}", MODEL_ROUTING_PATH=str(ROOT / "config/model_routing.yaml"),
        MODEL_ROUTING_OVERRIDES=json.dumps({"memory.extract": {"model": "gpt-6.1-sol", "catalog_model": "gpt-6.1-sol",
                                                               "reasoning_effort": "low"}}))
    os.environ["LLM_API_KEY"] = read_key(args.key_file)
    os.environ.pop("OPENAI_LOG", None)
    sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "services/company_os"), str(ROOT)]
    from app.chat.conversations import bind_conversation, bind_job_conversation
    from app.credits.ledger import append, verify_chain
    from app.credits.policy import WALLET_PHASE_KEY
    from app.credits.units import usd_to_micro
    from app.db.models import ChatConversation, ChatMessage, CreditAccount, CreditPolicy, Job, Person, Venture, Workspace, WorkspaceMember
    from app.memory.extraction import capture_chat_message
    from app.memory.facts import query_facts
    from app.memory.model_extract import capture_route, run, identifier
    from app.chat.memory_commands import fact_reply
    from app.state import db
    from app.tenancy import bind
    from core.clock import utcnow

    # Preflight public S06 APIs before any reservation or provider I/O.
    from llm_gateway.routing import resolve_purpose_config  # noqa: F401
    from llm_gateway.sole import transport_prompt_tokens  # noqa: F401
    factory = db()
    ceiling = usd_to_micro(args.budget_usd)
    person, wallet_id = "usr_memory_eval", "wallet_memory_eval"
    now = utcnow()
    with factory() as session:
        session.add(Person(id=person, display_name="Synthetic memory evaluator", created_at=now))
        wallet = CreditAccount(id=wallet_id, scope="person", owner_person_id=person, status="active",
            granted_micro=ceiling, spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now)
        session.add(wallet)
        session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"person"', updated_at=now, updated_by="synthetic_eval"))
        session.flush()
        append(session, account=wallet, kind="grant", amount_micro=ceiling, reason="Synthetic bounded eval allocation",
               actor_kind="system", actor_id="synthetic_eval", idempotency_key="eval-budget")
        session.commit()
    metrics = {"current_fact_cases": 0, "current_fact_correct": 0, "missing_cases": 0, "missing_abstained": 0,
               "history_cases": 0, "history_retained": 0, "source_quotes_checked": 0, "quotation_validation_escapes": 0}
    completed, calls_used, records = 0, 0, []
    status = "completed"
    for case in cases()[:args.max_calls]:
        cid = case["case_id"]
        wid, vid, conversation = "wsp_" + cid, "ven_" + cid, "cnv_" + cid
        final = case["text"]
        at = utcnow() - timedelta(minutes=2)
        final_id = "msg_" + cid + "_final"
        proof = {"schema_version": 1, "conversation_id": conversation, "workspace_id": wid, "venture_id": vid, "actor_id": person}
        payload = {"workspace_id": wid, "venture_id": vid, "source_message_id": final_id, "_actor_subject": person,
                   "_chat_conversation": proof, "_job_id": "job_" + cid, "_worker_id": "synthetic_eval", "_job_attempt": 1}
        with factory() as session:
            session.add(Workspace(id=wid, kind="venture", name="Synthetic memory case", entry_mode="idea", owner_id=person,
                                  created_at=at, updated_at=at))
            session.flush()
            session.add(WorkspaceMember(id="member_" + cid, workspace_id=wid, person_id=person,
                                        role="owner", can_approve=1, created_at=at))
            session.add(Venture(id=vid, workspace_id=wid, name="Synthetic memory case", raw_idea="Synthetic memory case",
                                stage="draft", created_at=at, updated_at=at))
            session.flush()
            session.add(ChatConversation(id=conversation, workspace_id=wid, venture_id=vid, creator_person_id=person,
                visibility="private", title="Synthetic private source", creation_key=cid, created_at=at, updated_at=at))
            session.flush()
            if case["category"] == "cross_turn":
                for index, audience in enumerate(case["prior_audiences"]):
                    session.add(ChatMessage(id="msg_" + cid + "_" + str(index), workspace_id=wid, venture_id=vid,
                        conversation_id=conversation, author_person_id=person, role="user", kind="text",
                        body_md=f"Our audience is {audience}.", created_at=at + timedelta(seconds=index)))
            session.add(ChatMessage(id=final_id, workspace_id=wid, venture_id=vid, conversation_id=conversation,
                author_person_id=person, role="user", kind="text", body_md=final, created_at=at + timedelta(seconds=2)))
            session.add(Job(id=payload["_job_id"], workspace_id=wid, venture_id=vid, kind="memory.extract",
                payload_json=json.dumps({key: value for key, value in payload.items() if key not in {"_job_id", "_worker_id", "_job_attempt"}}),
                status="leased", attempts=1, max_attempts=1, locked_by="synthetic_eval", created_at=at))
            session.commit()
        with bind(workspace_id=wid, venture_id=vid, actor=person), bind_conversation(vid, conversation):
            if case["category"] == "cross_turn":
                for index in range(2):
                    capture_chat_message("msg_" + cid + "_" + str(index))
            _, _, _, route = capture_route(wid, final)
            spent, protected, uncertain = _financials(factory)
            if time.monotonic() + 65 >= deadline:
                status = "clock_cap_halt"
                break
            if uncertain or not can_reserve(route["estimate_micro"], spent, protected, ceiling):
                status = "budget_or_unknown_outcome_halt"
                break
            try:
                with bind_job_conversation(payload, venture_id=vid, workspace_id=wid):
                    await run(payload)
            except Exception as exc:
                from app.memory.model_extract import read
                try:
                    calls_used += read(identifier(payload["_job_id"])).document["_runtime"]["calls_used"]
                except Exception:
                    pass  # A pre-I/O admission failure has no paid receipt to count.
                records.append({"case_id": cid, "status": "halted", "error_class": type(exc).__name__})
                status = "provider_or_receipt_halt"
                break
            completed += 1
            from app.db.models import VentureDocument
            with factory() as session:
                receipt = session.get(VentureDocument, identifier(payload["_job_id"]))
                runtime = json.loads(receipt.provenance_json)["_runtime"]
                calls_used += runtime["calls_used"]
                current = query_facts(session, venture_id=vid, subject=case["expected_subject"])
                history = query_facts(session, venture_id=vid, subject=case["expected_subject"], include_history=True)
                all_current = query_facts(session, venture_id=vid, limit=1000)
                all_history = query_facts(session, venture_id=vid, include_history=True, limit=1000)
                for fact in all_history:
                    source = json.loads(fact.source_json)
                    message = session.get(ChatMessage, source["ref"].split(":", 1)[1])
                    metrics["source_quotes_checked"] += 1
                    metrics["quotation_validation_escapes"] += int(fact.value != source["quote"] or source["quote"] not in message.body_md)
                if case["category"] == "missing":
                    metrics["missing_cases"] += 1
                    reply = fact_reply(vid, case["expected_subject"])["body_md"]
                    abstained = (not all_current and "don't have a source-backed statement" in reply
                                 or len(all_current) == len(current) == 1 and current[0].value in case["accepted_unknown_quotes"]
                                 and current[0].value in reply)
                    metrics["missing_abstained"] += int(abstained)
                    correct = abstained
                else:
                    metrics["current_fact_cases"] += 1
                    correct = bool(len(current) == 1 and current[0].value in case["accepted_current_quotes"]
                                   and json.loads(current[0].source_json)["ref"] == "chat:" + final_id
                                   and current[0].valid_from == (at + timedelta(seconds=2)).replace(tzinfo=None))
                    metrics["current_fact_correct"] += int(correct)
                    if case["category"] == "cross_turn":
                        metrics["history_cases"] += 1
                        metrics["history_retained"] += int(len(history) == 3)
                records.append({"case_id": cid, "status": "pass" if correct else "fail",
                                "source_digest": runtime["source"]["digest"]})
        spent, protected, uncertain = _financials(factory)
        if uncertain or spent > ceiling or calls_used > args.max_calls:
            status = "budget_or_unknown_outcome_halt"
            break
    spent, protected, uncertain = _financials(factory)
    chain_ok = verify_chain(wallet_id) == (True, None)
    return {"status": status, "evidence": "synthetic persisted private sources, actual handler/wallet, live captured Sol transport",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "completed_cases": completed, "calls_used": calls_used, "provider_actual_micro": spent,
            "protected_micro": protected, "budget_micro": ceiling, "ledger_verified": chain_ok,
            "metrics": metrics, "cases": records,
            "result_digest": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()}


def _execute_child(args, directory, sender):
    """Only the opt-in child loads the key or starts a transport."""
    try:
        sender.send(asyncio.run(execute(args, directory)))
    except Exception as exc:
        sender.send({"status": "preflight_or_runtime_halt", "error_class": type(exc).__name__})
    finally:
        sender.close()


def bounded_execute(args, directory, *, worker=_execute_child):
    """Own the whole-run deadline outside threads and phased HTTP timeouts."""
    started = time.monotonic()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(args, directory, sender))
    try:
        process.start()
        sender.close()
        remaining = max(0, HARD_SECONDS - (time.monotonic() - started))
        if receiver.poll(remaining):
            try:
                result = receiver.recv()
            except EOFError:
                result = {"status": "preflight_or_runtime_halt", "error_class": "ChildProcessError"}
            process.join(max(0, HARD_SECONDS - (time.monotonic() - started)))
            if not process.is_alive():
                return {**result, "elapsed_seconds": round(time.monotonic() - started, 2)}
        # The isolated DB and committed calling/usage receipts remain untouched.
        # Killing only this owned child prevents any further call or retry.
        if process.is_alive():
            process.kill()
            process.join(5)
        return {"status": "hard_deadline_halt", "outcome": "unverified",
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "receipt_db_path": str(directory / "eval.db"),
                "receipts_retained": True, "holds_released": False}
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        receiver.close()
        sender.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Make reviewed live provider calls; default only collects cases")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--max-calls", type=call_limit, default=30)
    parser.add_argument("--budget-usd", type=budget_usd, default=HARD_USD)
    args = parser.parse_args(argv)
    if not args.execute:
        _emit({"status": "collected_no_calls", "max_calls": args.max_calls, "budget_usd": str(args.budget_usd),
               "cases": [{"case_id": case["case_id"], "category": case["category"],
                          "script_digest": hashlib.sha256(case["text"].encode()).hexdigest()}
                         for case in cases()[:args.max_calls]]})
        return 0
    if args.key_file is None:
        parser.error("--key-file is required for reviewed execution")
    try:
        receipts = Path.home() / ".codex" / "handoffs" / "supymem-memory-eval"
        receipts.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="run-", dir=receipts))
        result = bounded_execute(args, directory)
        result["receipt_db_path"] = str(directory / "eval.db")
        _emit(result)
        return 0 if result["status"] == "completed" and result.get("ledger_verified") else 1
    except Exception as exc:
        _emit({"status": "preflight_or_runtime_halt", "error_class": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Source-only transports for the offline HTTP value gate."""

import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import sys
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

WORKFLOW = "We lose time reconciling updates in spreadsheets and notes."
OFFERING = "We publish a shared spreadsheet for tracking updates."
PARTNER_PATH = "Our community runs workshops and invites collaboration on this workflow."
FIXTURE_HOST = re.compile(r"group-([1-6])-[0-9a-f]{8}\.test")


def audience_for(scenario):
    # shortcut: the first sentence identifies the audience; add fixture metadata if scenario answers change format.
    answer = (scenario.get("answers") or [scenario["answer"]])[0]
    return answer.partition(". ")[0].rstrip(".")


def respond(request, state):
    messages = request["messages"]
    system = "\n".join(message["content"] for message in messages if message["role"] == "system")
    user = next(message["content"] for message in reversed(messages) if message["role"] == "user")
    if user == 'Reply with only this JSON object: {"connected":true}.':
        return {"connected": True}
    scenario = state.scenario
    audience = audience_for(scenario)
    if "idea clarifier" in system:
        question = "What do you use today to keep track of updates?"
        proposing = audience in user and "answered_questions" in user
        return {"case": "know", "action": "proposal" if proposing else "question",
            "confidence": 0.9 if proposing else 0.5,
            "message": "Review your idea and confirm it." if proposing else question,
            "understanding": {"offer": scenario["idea"], "summary": scenario["idea"], "customer": audience,
                "problem": "Reconciling updates across notes and spreadsheets.",
                "customer_extraction": {"text": audience, "kind": "unknown", "quote": audience},
                **dict.fromkeys(("first_user_path", "pricing_hypothesis", "workflow", "current_workaround", "why_now",
                    "evidence", "desired_outcome"), ""),
                **dict.fromkeys(("requirements", "constraints", "assumptions", "open_questions", "risks", "proposed_options"), [])},
            "question": None if proposing else {"field": "current_workaround", "text": question,
                "why": "Your design should fit their routine.", "options": ["Their current notes", "Their current spreadsheet"],
                "requires_detail": [], "detail_prompt": "", "selection_mode": "single", "option_labels": [],
                "recommended_option": "Their current notes", "recommendation_reason": "Pick this if notes describe your current method."}}
    if "Plan searches for prior attempts" in system:
        category = " ".join(audience.split()[:4])
        return {"category_terms": [category], "job_phrases": ["reconciling updates"], "customer_terms": [],
                "seed_competitors": [], "negative_terms": [], "consumer_mobile": False}
    if "You review evidence" in system:
        payload = prompt_json(messages, "sources")
        sources = [source for source in payload["sources"] if source["excerpts"]]
        competitor = next(((source["id"], index) for source in sources if source["page_kind"] == "offering"
                           for index in source["fetched_quote_indexes"] if OFFERING in source["excerpts"][index]), None)
        return {"overview": "Public sources show a few approaches to reconciling updates.",
            "observations": [{"source_id": source["id"], "quote_index": 0} for source in sources], "assessments": [],
            "service_checks": [], "customer_reviews": [],
            "competitor_checks": [{"source_id": competitor[0], "product_quote_index": competitor[1],
                "customer_quote_index": None, "positioning_quote_index": None, "pricing_quote_index": None,
                "competition_type": "indirect", "comparable_item_quote_index": None, "rating_quote_index": None,
                "review_count_quote_index": None}] if competitor else [],
            "coverage_gaps": ["Interviews are needed to confirm demand."],
            "proposed_tests": ["Ask two people to describe their most recent workaround."]}
    if "Plan at most eight varied search queries" in system or "Return JSON queries with query and intent" in system:
        payload = prompt_json(messages, "who", "saved_audience", "target_type")
        who = " ".join(str(payload.get("who") or payload.get("saved_audience") or audience).split()[:10])
        if "The attempted queries returned no public candidates" in system:
            searches = [(f"{role} contact {' '.join(who.split()[:5])}", "person", role)
                        for role in ("Workshop organizer", "Community educator")]
        else:
            searches = [(f"{who} {terms}", intent, None) for terms, intent in (
                ("contact workflow", "business"), ("contact team", "business"),
                ("contact practitioners", "business"), ("reconciling updates forum", "customer_voice"))]
        return {"queries": [{"query": query, "intent": intent, "expert_role": role} for query, intent, role in searches],
                "target_type": None, "audience": who, "requested_count": None, "requested_role": "", "organisation_audience": ""}
    if "Prepare only the private market-test plan" in system:
        return {"hypothesis": f"At least two people in this audience lose time reconciling updates: {audience}.",
                "action": "Over one week, invite five people with permission to a 10-minute interview. Ask them to describe the last time they reconciled updates. Record their answers without asking them to buy anything.",
                "success_measure": "Pass if at least two describe the problem from a recent experience. Fail if none do. Otherwise or with fewer than five interviews, mark it inconclusive."}
    if "Extract an experiment result" in system:
        return {"ran": True, "outcome": "inconclusive", "learning": "You reported interest in reviewing a prototype.",
                "complete": True, "criteria_met": False, "criteria_failed": False}
    if "Rank EVERY supplied passage" in system:
        return {"items": [{"id": row["id"], "relevance": 0.9 if WORKFLOW in row["text"] else 0.1}
                          for row in prompt_json(messages, "candidates")["candidates"]]}
    if "role_in_market" in system and "homepage" in system:
        candidate = prompt_json(messages, "candidates")["candidates"][0]
        quote = WORKFLOW if WORKFLOW in candidate["text"] else candidate["text"].split(". ", 1)[0]
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False, "homepage_quote": quote}
    if "Score each reachable lead" in system:
        candidates = prompt_json(messages, "candidates")["candidates"]
        items = []
        for candidate in candidates:
            page = next((page for page in candidate["evidence_pages"] if WORKFLOW in page["text"]), None)
            if not page:
                items.append({"id": candidate["id"], "reject_reason": "low_fit"})
                continue
            name = source_text(candidate["title"]).partition(":")[0]
            items.append({"id": candidate["id"], "kind": source_text(candidate["kind"]), "name": name,
                "affiliation": "", "url": source_text(page["url"]), "quote": WORKFLOW,
                "role": "", "company": name, "location": "", "fit": 88,
                "partner_quote": PARTNER_PATH if PARTNER_PATH in page["text"] else "",
                "why": "Their published work matches the workflow.",
                "value_exchange": "Compare notes on this workflow in a short research call."})
        return {"items": items}
    if "Judge each candidate independently" in system:
        return {"items": [{"id": candidate["id"], "reject_reason": "No independently dated signal."}
                          for candidate in prompt_json(messages, "candidates")["candidates"]]}
    if "Write a short personalised unsent note" in system:
        return {"items": [{"id": candidate["id"],
            "body": f"Hi {source_text(candidate['recipient_name'])}, could we compare notes about your workflow? Reply with a time.",
            "connection_note": "Could we compare notes about your workflow?"}
            for candidate in prompt_json(messages, "candidates")["candidates"]]}
    if "You write short first-contact emails" in system:
        marker = "Write one email per recipient below; reference the specific business or thing found about them.\n"
        prompt = next(message["content"] for message in messages if marker in message["content"])
        recipients, _ = json.JSONDecoder(strict=False).raw_decode(prompt.split(marker, 1)[1])
        minutes = re.search(r"End with ONE concrete ask: a (\d+)-minute call", system)[1]
        return {"drafts": [{"to": source_text(recipient["to"]),
                            "subject": "Could we compare notes?",
                            "body": f"Hi, I'm exploring how people keep track of updates across notes and spreadsheets. Would you be open to a {minutes}-minute call to share how you do this today?"}
                           for recipient in recipients]}
    if "one short first-contact email" in system:
        payload = prompt_json(messages, "template")
        return {"subject": payload["template_subject"], "body": payload["template"]}
    if "product phrase that completes" in system:
        return " ".join(scenario["idea"].split()[:16]).rstrip(",.").lower()
    if "Pick the 2 or 3 outreach playbooks" in system:
        return {"playbooks": [{"key": "problem_interview", "why": "You asked to learn from interviews."},
                             {"key": "community_intro", "why": "A community can introduce you to practitioners."}]}
    if "You add an optional diagram" in system:
        return {"draw": False}
    if "independent skeptical factual capability reviewer" in system:
        return state.page_review(next(message["content"] for message in messages if "CONFIRMED_FACTS:\n" in message["content"]))
    if "You route a startup founder's chat message" in system:
        text = prompt_json(messages, "founder_message")["founder_message"]
        return {"acts": [{"intent": "assistant", "text": text}], "clarify": None}
    return "Ask two more people how they handle this today, then record what they tell you."


def source_text(value):
    return unescape(value.removeprefix("<untrusted>").removesuffix("</untrusted>").strip())


def prompt_json(messages, *keys):
    for message in messages:
        if message["role"] != "user":
            continue
        try:
            value, _ = json.JSONDecoder(strict=False).raw_decode(message["content"].lstrip())
        except ValueError:
            continue
        if isinstance(value, dict) and any(key in value for key in keys):
            return value
    raise ValueError("The fixture did not find the expected model input fields")


def install_transports(data):
    from app.core import local_env_import
    from app.research import local_providers
    from app.signals import local_reader
    from core.clock import utcnow
    from desktop_runtime import agent_status, owned_coding

    local_env_import.repo_root = lambda: data
    binary = data / "bin" / "codex"
    safe_directory = owned_coding._safe_directory

    def fixture_directory(path):
        if path != binary.parent:
            return safe_directory(path)
        return all(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
                   for info in (data.lstat(), path.lstat()))

    owned_coding._safe_directory = fixture_directory
    agent_status.provider_binary = lambda name, path_env=None: str(binary) if name == "codex" else None
    resolve = socket.getaddrinfo

    def resolve_fixture(host, port, *args, **kwargs):
        if isinstance(host, str) and FIXTURE_HOST.fullmatch(host):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.215.14", port))]
        return resolve(host, port, *args, **kwargs)

    socket.getaddrinfo = resolve_fixture

    def scenario():
        return json.loads((data / "journey-fixture.json").read_text(encoding="utf-8"))

    def search(query, limit, deadline):
        saved = scenario()
        scope = hashlib.sha256(saved["idea"].encode()).hexdigest()[:8]
        results = []
        for index in range(1, min(limit, 6) + 1):
            host = f"group-{index}-{scope}.test"
            results.append({"href": f"https://{host}/contact",
                "title": f"Community {index}: " + (saved["idea"] if index == 1 else "public contact"),
                "body": page_text(index, host, saved)})
        return results

    def page_text(index, host, saved):
        name = f"Community {index}"
        description = f"{name}: Our members are {audience_for(saved)}. {WORKFLOW}"
        if index == 1:
            return f"{description} {OFFERING} {saved['idea']}."
        return (f"{description} {PARTNER_PATH} "
                f"Our group can be contacted at team@{host}. Reach out to discuss our workflow or share experience.")

    def page(url, main_only):
        host = urlsplit(url).hostname
        match = FIXTURE_HOST.fullmatch(host or "")
        if not match:
            raise ValueError("Use a registered fixture URL in the offline journey")
        index = int(match[1])
        return local_reader._page_response(url, page_text(index, host, scenario()), f"Community {index}",
                                           metadata={"published_at": utcnow().isoformat()})

    local_providers._builtin_search_sync = search
    local_reader._basic_read = page


def block_external_network(event, args):
    if event not in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        return
    address = args[1] if event in {"socket.connect", "socket.sendto"} else args[0]
    if isinstance(address, tuple):
        address = address[0]
    elif event in {"socket.connect", "socket.sendto"}:
        return
    try:
        if address in (None, "localhost") or ipaddress.ip_address(address).is_loopback:
            return
    except ValueError:
        pass
    print("FAIL offline network: Check the journey's fixture transports before retrying.", flush=True)
    raise OSError("External network is disabled in the offline journey")


def main(role):
    from core.desktop_env import is_packaged_desktop

    if is_packaged_desktop() or any(os.environ.get(flag) != "1" for flag in (
            "SUPYMEM_TEST_OFFLINE_JOURNEY", "SUPYMEM_TEST_FAKE_CODING_TOOL")) or role not in {"api", "worker"}:
        return 1
    sys.addaudithook(block_external_network)
    os.environ["APP_ROLE"] = role
    install_transports(Path(os.environ["COMPANY_OS_DATA_DIR"]))
    if role == "worker":
        from app.worker.main import main as worker_main

        return worker_main([])

    import uvicorn
    from app.state import db

    db()
    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ["CORE_PORT"]), proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

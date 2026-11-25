"""Founder-facing belief cards and the single plan approval."""
from __future__ import annotations

import asyncio
import json

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.chat.recovery import founder_failure
from app.core.local_mode import is_local
from app.credits.units import format_credits, format_usd
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.experiments import autonomous, belief_autopilot, belief_planner, beliefs, ledger
from app.state import db
from core.copy import founder_copy


async def prepare(venture_id, payload=None):
    try:
        return await _prepare(venture_id, payload)
    except beliefs.IdeaIntakePending:
        return await asyncio.to_thread(intake_question, venture_id)
    except Exception as exc:
        from llm_gateway.errors import ProviderCallError

        if (payload or {}).get("day_one"):
            # Nobody asked for this card: log it and stay quiet instead of showing a failure.
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "experiment.beliefs.prepare")
            return None
        retry = {"action": "experiment.beliefs.prepare", "payload": payload or {}}
        if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
            retry = {"action": "status", "payload": {}}
        return founder_failure(exc, venture_id, "your market tests",
            retry,
            safety="Your idea is saved and nothing was launched.")


async def _prepare(venture_id, payload):
    from app.ventures.current_idea import intake_pending

    if await asyncio.to_thread(intake_pending, venture_id):
        return await asyncio.to_thread(intake_question, venture_id)
    snapshot = None
    if belief_autopilot.enabled():
        from app.chat.experiments import audience_question
        snapshot = await asyncio.to_thread(autonomous._snapshot, venture_id)
        if snapshot and not (snapshot["journey"].get("icp") or {}).get("who"):
            return await asyncio.to_thread(audience_question, venture_id,
                request={"action": "experiment.beliefs.prepare", "payload": payload or {}})
    context = await beliefs.generate(venture_id)
    actions = [{"label": "Open your market map", "href": f"/v/{venture_id}/market-map"}]
    if context.get("plan", {}) and context["plan"]["state"] in {"active", "budget_exhausted"}:
        actions.append({"label": "Stop new market tests", "action": "experiment.beliefs.stop"})
        return {"kind": "market_beliefs", "body_md": "Your market beliefs and current tests.", "actions": actions, "citations": []}
    experiments = await asyncio.to_thread(belief_planner.available, venture_id, context["beliefs"])
    inputs = await asyncio.to_thread(ledger.ranking_inputs, venture_id)
    options = (payload or {}) if belief_autopilot.enabled() else {}
    plan = belief_planner.build(context["beliefs"], experiments, inputs,
        ceiling_micro=options.get("weekly_budget_micro"), max_emails_per_day=options.get("max_emails_per_day", 15))
    if plan["tests"]:
        from app.credits.estimate import estimate_llm_purpose_micro
        followup_budget = estimate_llm_purpose_micro("experiment.beliefs", prompt_tokens=6000, max_tokens=beliefs.DRAFT_MAX_TOKENS)
        plan["ceiling_micro"] += plan["ceiling_micro"] + len(plan["tests"]) * followup_budget
        plan["fingerprint"] = autonomous._fingerprint({key: value for key, value in plan.items() if key != "fingerprint"})
    if belief_autopilot.enabled() and plan["tests"]:
        selected = [spec for spec in experiments if any(test["experiment_id"] == spec["id"] for test in plan["tests"])]
        methods = sorted({belief_planner.runner(spec) for spec in selected})
        plan.update(renewal="weekly_until_revoked", expires_at=None, revoked_at=None,
            weekly_budget_micro=options.get("weekly_budget_micro", plan["ceiling_micro"]),
            allowed_methods=options.get("allowed_methods", methods),
            allowed_audiences=options.get("allowed_audiences", sorted({spec.get("icp", {}).get("who", "") for spec in selected})),
            max_emails_per_day=options.get("max_emails_per_day", plan["max_emails_per_day"]),
            direction=inputs.get("direction", selected[0].get("direction", "")), calls=[])
        belief_autopilot.validate_standing(plan)
        if set(plan["allowed_methods"]) != set(methods):
            raise ValueError("Your allowed methods must match the tests on your approval card.")
        plan["ceiling_micro"] = plan["weekly_budget_micro"]
        plan["fingerprint"] = autonomous._fingerprint({key: value for key, value in plan.items() if key != "fingerprint"})
    with db()() as session:
        serialize_writes(session, key="market-beliefs:" + venture_id)
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        if context.get("plan") and context["plan"]["state"] in {"active", "budget_exhausted"}:
            return {"kind": "market_beliefs", "body_md": "Your market plan is already approved.", "actions": actions, "citations": []}
        context["plan"] = plan
        for test in plan["tests"]:
            if session.get(Experiment, test["experiment_id"]) is None:
                spec = next(spec for spec in experiments if spec["id"] == test["experiment_id"])
                session.add(Experiment(id=spec["id"], venture_id=venture_id, workspace_id=saved.workspace_id,
                    kind=ledger.KIND, hypothesis=spec["hypothesis"], action=spec["method"], state="proposed",
                    context=json.dumps({"runner": "system", "spec": spec})))
        saved.context = json.dumps(context)
        session.commit()
    if plan["tests"]:
        interpretation = (f"I'll test these {len(plan['tests'])} market questions over 7 days. Your limit is up to "
            f"{format_credits(plan['ceiling_micro'])} credits and {plan['max_emails_per_day']} emails/day.")
        if plan.get("renewal"):
            interpretation = ("Your tests renew every 7 days until you stop them. "
                f"Your weekly budget in credits: {format_credits(plan['weekly_budget_micro'])}. "
                f"Your email cap is {plan['max_emails_per_day']} per day.")
        card = ConfirmationCard.for_action(get_action("experiment.beliefs.approve"), interpretation=interpretation,
            consequences=[f"{test['hypothesis']} Test: {test['method']} Sample: {test['sample_size']}. "
                          f"Pass: {test['metric'].replace('_', ' ')} {test['success_threshold']['op']} {test['success_threshold']['value']}. "
                          f"Fail: {test['metric'].replace('_', ' ')} {test['failure_threshold']['op']} {test['failure_threshold']['value']}. "
                          f"Up to {format_credits(test['ceiling_micro'])} credits." for test in plan["tests"]] + [
                ("Your unused weekly budget does not roll over. Unsettled work stays reserved. Stop new market tests to revoke renewal and new experiment emails. "
                 f"Allowed methods: {'; '.join(sorted({test['method'] for test in plan['tests']}))}. Audiences: {', '.join(plan['allowed_audiences']) or 'your saved test scope'}."
                 if plan.get("renewal") else "Your budget includes follow-up beliefs and tests. New steps stop at your credit ceiling or after 7 days."),
                "Email tests run with the Automate all emails consent you already gave. Pausing sending pauses those tests. Your daily cap is never raised.",
                "Every email keeps public-source or founder-import disclosure, opt-out and your sender address. Suppression, eligibility and your daily cap stay in place.",
                "Publishing and Mac read-only tasks keep their existing approvals. No LinkedIn sending, paid ads, deposits or other real-money spending."],
            resulting_state={"plan_id": plan["id"], "fingerprint": plan["fingerprint"]})
        action = card.as_action()
        action["label"] = "Run experiments"
        if options.get("day_one") is True and plan["max_emails_per_day"] == 0:
            explanations = {
                "research": ("Research", "Check public sources before you choose your next step."),
                "response": ("Market response", "See how people respond to your idea."),
                "challenge": ("Challenge", "Look for reasons your idea might not work."),
            }
            summaries, featured = [], {}
            for test in plan["tests"]:
                spec = next(spec for spec in experiments if spec["id"] == test["experiment_id"])
                lane = belief_planner._portfolio_lane(spec)
                title, why = explanations[lane]
                contacts = "yes" if not belief_planner.observing(spec) and (
                    spec.get("approval") in {"each_send", "each_publish"} or
                    belief_planner.runner(spec) == "wizard_tests") else "no"
                summaries.append(f"{title}: {test['hypothesis']}\n"
                    f"Why: {why} Time: up to {spec.get('max_duration_days', plan['days'])} days. "
                    f"Cost: up to {format_usd(test['ceiling_micro'])}. Contacts people: {contacts}.")
                featured.setdefault(lane, summaries[-1])
            interpretation = "Here is your first experiment plan\n\n" + "\n\n".join(
                f"{index}. {summary}" for index, summary in enumerate(
                    [featured[lane] for lane in explanations if lane in featured], 1))
            if "response" not in featured:
                interpretation += ("\n\nYour market-response experiment waits for an approved way to measure "
                    "real responses. No one is contacted.")
            if "challenge" not in featured:
                interpretation += "\n\nYour challenge experiment waits for a runnable test of what could go wrong."
            if len(summaries) > len(featured):
                # shortcut: approval covers the full portfolio; narrow it when the planner supports a day-one limit.
                interpretation += "\n\nYour weekly approval also lists the remaining planned tests."
            interpretation += "\n\nOnce you approve, your zero-contact research runs automatically. Your email allowance starts at 0 per day."
            interpretation = founder_copy(interpretation)
            action["label"] = "Approve weekly experiment budget"
            action["confirmation"]["interpretation"] = (
                f"Your weekly experiment allowance is up to {format_usd(plan['weekly_budget_micro'])}. "
                "It renews every 7 days until you stop it. Your email allowance is 0 per day.")
            action["confirmation"]["consequences"] = [founder_copy(text) for text in summaries + [
                "Your allowance includes follow-up research. Your unused allowance does not roll over. "
                "Work already started counts against your allowance until it finishes.",
                "Your approved methods: " + "; ".join(sorted({test["method"] for test in plan["tests"]})) +
                ". Your approved audiences: " + ", ".join(plan["allowed_audiences"]) + ".",
                "Stop new market tests to stop weekly renewal and new experiment emails. Already approved work may finish.",
                "You must approve a higher email allowance before any experiment emails can be sent. "
                "Your existing sending consent, daily limits, suppression and eligibility checks still apply.",
                "Every email keeps public-source or founder-import disclosure, opt-out and your sender address.",
                "Publishing and read-only computer tasks keep their existing approvals. "
                "No automated LinkedIn messages, paid ads, deposits or other purchases."]]
        if is_local():
            interpretation = "Your planned market tests:\n" + "\n".join(
                f"- {test['hypothesis']} {test['method']}" for test in plan["tests"])
            if plan.get("allowed_audiences"):
                interpretation += "\nYour audience: " + "; ".join(plan["allowed_audiences"]) + "."
            if plan.get("renewal"):
                interpretation += ("\nThese tests repeat every seven days until you choose Stop new market tests. "
                    "Only the shown methods and audiences can be used.")
            interpretation += (f"\nYour email cap is {plan['max_emails_per_day']} per day. "
                "Emails keep source disclosure, opt-out and your sender address; suppression and send caps still apply. "
                "Nothing starts until you ask to run these tests. Choose Run your market tests to start.")
            action = {"label": "Run your market tests", "action": "experiment.beliefs.approve",
                "payload": {"plan_id": plan["id"], "fingerprint": plan["fingerprint"]}}
        actions.insert(0, action)
    else:
        interpretation = "Your beliefs are ready. Your next tests need setup before you can approve a seven-day plan."
    from app.chat.experiments import audience_note
    note, audience_actions = audience_note(snapshot, request={"action": "experiment.beliefs.prepare", "payload": payload or {}})
    from app.experiments import tools
    if tools.enabled():
        selected = next((item for item in plan["waiting"] if item.get("tool")), None)
        if selected:
            inventory = tools.capabilities(venture_id, experiments)
            entry = next((item for item in inventory["tools"] if item["slug"] == selected["tool"]), None)
            if entry and not entry["connected"]:
                setup = tools.connection_reply(selected["tool"], selected["reason"], reconnect=entry["publishing_connected"] or entry["needs_reconnect"])
                interpretation += "\n\n" + setup["body_md"]
                actions += setup["actions"]
            else:
                interpretation += "\n\n" + founder_copy(selected["reason"])
            actions.append({"label": "Prepare your chosen tool test", "action": "experiment.prepare",
                "payload": {"experiment_id": selected["experiment_id"]}})
    return {"kind": "market_beliefs", "body_md": note + interpretation, "actions": actions + audience_actions, "citations": []}


def intake_question(venture_id):
    from app.chat.build_path import startup_review_reply
    from app.chat.idea_proposal import review
    from app.db.models import Venture
    from app.ventures.build_prerequisites import answers_pending

    with db()() as session:
        venture = session.get(Venture, venture_id)
        review_pending = venture and answers_pending(session, venture)
    if review_pending:
        return startup_review_reply(venture_id)

    reply = review(venture_id)
    reply["actions"] = [action for action in reply["actions"] if not action.get("action", "").startswith("experiment.")]
    if not reply["actions"]:
        reply["body_md"] += "\n\nLet's finish understanding your idea before planning market tests."
        reply["actions"] = [{"label": "Continue idea review", "action": "idea.review", "payload": {}}]
    return reply


async def execute(venture_id, name, payload):
    if name == "experiment.beliefs.prepare":
        return await prepare(venture_id, payload)
    if name == "experiment.beliefs.stop":
        context = await asyncio.to_thread(belief_autopilot.stop, venture_id)
        body = ("Your weekly renewal and new experiment emails are stopped. Already approved work may finish."
                if (context.get("plan") or {}).get("renewal") else
                "Your new market tests are stopped. Already approved work may finish. Say pause sending to stop new emails.")
        return {"kind": "text", "body_md": body, "actions": [], "citations": []}
    await asyncio.to_thread(belief_autopilot.approve, venture_id, payload)
    return {"kind": "market_beliefs", "body_md": "Your market plan is approved. Your tests will run within your saved budget; email tests keep your existing sending consent.",
        "actions": [{"label": "Open your market map", "href": f"/v/{venture_id}/market-map"},
                    {"label": "Stop new market tests", "action": "experiment.beliefs.stop"}], "citations": []}

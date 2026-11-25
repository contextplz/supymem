"""Phase 5 landing-page brief, connected-tool admission, and checked ingestion."""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.db.models import ArchitectureDecisionRecord
from app.core.local_mode import is_local
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx
from app.state import MEM, db
from core.clock import utcnow
from core.ids import new_id


CLOUD_UNAVAILABLE_MSG = (
    "Cloud builder is not open yet. Nothing was built. "
    "Use Cursor, Claude Code, or Codex on your Mac."
)


def _local_builder_caller():
    from app.admin.authn import CallerContext
    from app.core.local_owner import current_owner

    owner = current_owner()
    return CallerContext(sub=owner.id, email=owner.email or "", role="user", path="", method="POST", exp=0, nonce="")


def _local_builder_order(venture_id: str, run) -> dict | None:
    from app.api.device_orders_api import invoke
    from app.build.device_orders import retrieve

    order_id = get_run_gate("builder_device_order", venture_id, run.id) if run else None
    if not order_id:
        return None
    return invoke(retrieve, venture_id, _local_builder_caller(), order_id)


def builder_cancel_device_order(venture_id: str, run) -> None:
    from app.api.device_orders_api import invoke
    from app.build.device_orders import transition

    order = _local_builder_order(venture_id, run)
    if order and order["state"] in {"pending", "approved", "claimed", "waiting_for_device"}:
        invoke(transition, venture_id, _local_builder_caller(), order["id"], order["review_digest"], "cancel")


def _local_order_verified(order: dict | None) -> bool:
    if not order:
        return False
    coding = order.get("manifest", {}).get("coding_verification") or {}
    stop = order.get("native_stop") or {}
    return bool(order.get("state") == "stopped" and order.get("closed_at") and order.get("execution_id")
                and coding.get("provider") in LOCAL_ADAPTERS and coding.get("operation") == "workspace_edit_v1"
                and stop.get("execution_id") == order["execution_id"] and stop.get("task_verified") is True
                and stop.get("processes_stopped") is True and stop.get("verification_kind") == "workspace_edit_v1"
                and not any(order.get(field) for field in ("cancelled_at", "stopped_by_user", "stop_reconciliation_required",
                                                         "inference_reconciliation_required")))


def _local_build_pause(ctx: Ctx, *, provider: str | None = None) -> StepResult:
    from app.builder.ctl import local_tool, request_local_build
    from app.onboarding.registry import PauseRequest

    if ctx.cancel.is_set():
        from app.onboarding.failures import StepError

        raise StepError("cancelled", "Resume your venture in its chat, then ask to build your landing page again.")
    try:
        prompt = builder_prompt_for(ctx.venture)
        order = _local_builder_order(ctx.venture.id, ctx.run)
        if order and provider and (order.get("manifest", {}).get("coding_verification") or {}).get("provider") != provider:
            raise HTTPException(409, detail={"code": "builder_already_running", "message":
                "Open Builds and stop your current build before choosing another coding tool."})
        if not order:
            provider = local_tool(ctx.venture.id, provider=provider)
            order = request_local_build(ctx.venture.id, "Build my landing page from my saved idea and approved plan.",
                                        provider=provider, repair=True, brief=prompt)
            set_run_gate("builder_session", ctx.venture.id, ctx.run.id, "pending")
            set_run_gate("builder_device_order", ctx.venture.id, ctx.run.id, order["id"])
        if get_run_gate("builder_session", ctx.venture.id, ctx.run.id) == "done" and _local_order_verified(order):
            from app.firstlook.service import landing_snapshot

            saved = landing_snapshot(ctx.venture.id, _workspace_id(ctx), repo=_repo_dir(ctx))
            if saved:
                return StepResult(output=saved, detail=saved.get("files"))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = detail.get("message") or "Open Builds in your venture, check your saved scope, then retry your landing page."
        connect = detail.get("code") == "build_tool_needs_setup"
        if connect:
            message = message.replace("Connections > Coding tools", "Settings > Keys and connections > Coding tools")
        action = "/connections" if connect else f"/v/{ctx.venture.id}"
        return StepResult(output={"files": [], "message": message, "error": message,
                                  "actions": [{"label": "Connect your coding tool" if connect else "Open your venture",
                                               "href": action, "style": "primary"}]}, partial=True,
                          pause=PauseRequest(reason="awaiting_builder_session", founder_action=action))
    path = order["review_path"]
    message = "Your landing-page request is ready for your coding tool. Open Builds to follow it and check the result."
    if order["state"] in {"stopped", "cancelled", "expired"}:
        message = "Your build stopped. Open Builds to check its result before retrying or importing your checked page."
    return StepResult(output={"files": [], "prompt": prompt, "message": message,
                              "device_work_order_id": order["id"], "review_path": path}, partial=True,
                      pause=PauseRequest(reason="awaiting_builder_session", founder_action=path))


def cloud_builder_live() -> bool:
    """Hosted Fargate/OpenHands is Phase 3. Default is off so we never fake a page."""
    if (os.environ.get("BUILDER_HOSTED") or "").strip().lower() in {"1", "true", "yes"}:
        return True
    mode = (os.environ.get("BUILDER_SANDBOX") or "").strip().lower()
    return mode == "fargate"


def _build_provider() -> str:
    """BUILD_PROVIDER wins; else CODING_AGENT; default stub path (unchanged)."""
    return (
        (os.environ.get("BUILD_PROVIDER") or os.environ.get("CODING_AGENT") or "stub")
        .strip()
        .lower()
    )


def _workspace_id(ctx: Ctx) -> str | None:
    run = getattr(ctx, "run", None)
    venture = getattr(ctx, "venture", None)
    return getattr(run, "workspace_id", None) or getattr(venture, "workspace_id", None)


def _interest_action(ctx: Ctx) -> str:
    wid = _workspace_id(ctx)
    if not wid:
        return "#"
    from app.firstlook.waitlist import public_interest_url

    return public_interest_url(wid)


def _waitlist_form_html(ctx: Ctx, *, version: int = 1) -> str:
    action = _interest_action(ctx)
    return f"""
  <form id="interest-form" method="post" action="{action}">
    <input type="hidden" name="site_version" value="{version}"/>
    <input type="hidden" name="source" value="direct"/>
    <input name="email" type="email" placeholder="you@example.com" required autocomplete="email"/>
    <button type="submit">Join the waitlist</button>
  </form>
  <p id="interest-thanks" hidden style="opacity:.85">Thanks, that's recorded.</p>
  <script>
  (function(){{
    var form=document.getElementById('interest-form');
    var thanks=document.getElementById('interest-thanks');
    if(!form||!thanks||form.action.endsWith('#')) return;
    form.addEventListener('submit',function(e){{
      e.preventDefault();
      var b=form.querySelector('button');
      b.disabled=true;
      fetch(form.action,{{method:'POST',mode:'cors',credentials:'omit',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:form.email.value,note:(form.note&&form.note.value)||'',source:'direct',site_version:{version}}})}})
        .then(function(r){{ if(!r.ok) throw new Error('x'); form.hidden=true; thanks.hidden=false; }})
        .catch(function(){{ b.disabled=false; form.submit(); }});
    }});
  }})();
  </script>
"""


def _queue_computer_use(ctx: Ctx, company: str, one_liner: str) -> None:
    """When the plan says build, the Mac app starts Computer Use — no founder picker."""
    try:
        from app.build.computer_use import enqueue_computer_use
        from app.build.device_orders import desktop_execution_enabled

        # Native Mac execution is default-off (exact "1" gate); never queue a job nobody may run.
        if not desktop_execution_enabled():
            return
        enqueue_computer_use(
            ctx.venture.id,
            title=f"Build {company}",
            prompt=(
                f"Build this product on this Mac. Name: {company}. "
                f"{one_liner} Idea: {ctx.venture.raw_idea}. "
                "Use the venture repo. Produce a working first version, not a slide deck."
            ),
        )
    except Exception:
        return


def _repo_dir(ctx: Ctx) -> Path:
    from app.config import data_root

    root = data_root() / "ventures" / ctx.venture.id / "repo"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def draft_adr(ctx: Ctx) -> StepResult:
    stack = ["nextjs", "sqlite"]
    adr = {
        "title": "MVP landing page",
        "stack": stack,
        "protected_routes": [],
        "components_estimate": 4,
    }
    charter_id = (ctx.out.get("commit.lock_charter") or {}).get("charter_id") or ""
    factory = db()
    with factory() as session:
        session.add(
            ArchitectureDecisionRecord(
                id=new_id("adr"),
                venture_id=ctx.venture.id,
                charter_id=charter_id or "pending",
                version=1,
                locked=1,
                stack_json=json.dumps(stack),
                schema_json=json.dumps({"tables": ["users", "waitlist"]}),
                api_contract_json=json.dumps({"routes": ["/"]}),
                auth_flow_json=json.dumps({"mode": "none"}),
                error_strategy_json=json.dumps({"mode": "friendly"}),
                deploy_topology_json=json.dumps({"target": "local"}),
                component_inventory_json=json.dumps({"landing": 1}),
                created_at=utcnow(),
                locked_at=utcnow(),
            )
        )
        session.commit()
    MEM[f"adr:{ctx.venture.id}"] = adr
    return StepResult(output=adr, tokens={"stack_word": "a simple web stack"})


async def plan_work_orders(ctx: Ctx) -> StepResult:
    orders = [
        {"id": "wo_landing", "title": "Landing page", "kind": "landing"},
        {"id": "wo_copy", "title": "Hero copy", "kind": "copy"},
        {"id": "wo_cta", "title": "Waitlist CTA", "kind": "cta"},
    ]
    return StepResult(
        output={"orders": orders, "landing_work_order": orders[0], "count": len(orders)},
        tokens={"count": len(orders)},
    )


def landing_builder_prompt(
    company: str,
    one_liner: str,
    idea: str,
    brief: dict | None = None,
    waitlist_action: str = "#",
) -> str:
    """Same first-look contract Cloud uses — waitlist, honest copy, keep going."""
    action = (waitlist_action or "#").strip() or "#"
    parts = [
        f"Build a complete first-look landing page for {company}.",
        f"One line: {one_liner}." if one_liner else "",
        f"Idea (use these words in the footer): {idea}." if idea else "",
    ]
    if isinstance(brief, dict):
        heard = brief.get("understood") or brief.get("what_it_is")
        if heard:
            parts.append(f"The product I understood: {heard}.")
        if brief.get("first_user"):
            parts.append(f"First person: {brief['first_user']}.")
        if brief.get("first_job"):
            parts.append(f"Must work first: {brief['first_job']}.")
    parts.append(
        "This is the default Supymem first-look page, not a thin teaser. "
        "Keep going across as many turns as you need until a real page is ready to preview. "
        "Do not stop after one file."
    )
    parts.append(
        "Deliver a self-contained index.html (styles.css only if needed). "
        "No framework, no build step, no npm, no CDN, no Google fonts, no stock photos, no fake UI screenshots. "
        "CSS-only motion with prefers-reduced-motion. Mobile-friendly."
    )
    parts.append(
        "Required sections, in order: "
        "(1) wordmark/nav with in-page links to How it works and Waitlist; "
        "(2) hero, headline, subhead, primary CTA 'Join the waitlist' that jumps to #interest; "
        "(3) exactly three value points specific to THIS idea; "
        "(4) how it works, three short steps; "
        "(5) waitlist capture at #interest; "
        "(6) four FAQ items drawn from honest assumptions, not invented traction; "
        "(7) footer with a first-look disclaimer, the founder's own words, and a version line."
    )
    destination_rule = ("(use that exact destination, including loopback URLs; never invent a replacement). " if is_local() else
                        "(that exact https URL, never localhost, never a relative path). ")
    parts.append(
        f"Waitlist form: POST JSON {{email, note, source, site_version}} to {action} "
        f"{destination_rule}"
        "Fields: email (required), note (optional textarea), "
        "hidden site_version=1, hidden source=direct. Button label 'Join the waitlist'. "
        "On success hide the form and show 'Thanks, that's recorded.' "
        "Privacy line under the form: 'Stored on this machine only. Never sent to a model.'"
    )
    parts.append(
        "Do not invent testimonials, logos, user counts, revenue, press, partners, prices, or guarantees. "
        "No 'trusted by', 'AI-powered', 'seamless', 'best', or '#1'. "
        "If you would show social proof and have none, omit that section. "
        "Pricing stays off unless the founder already named a price, then label it early access."
    )
    parts.append(
        "Work in the current repo. Tell me what you changed as you go. "
        "When index.html is preview-ready, serve THIS folder on loopback so the founder can review it: "
        "python3 -m http.server 8765 --bind 127.0.0.1 "
        "(if 8765 is taken, pick another port in 8765-8799). "
        "Print exactly one line: Local preview: http://127.0.0.1:<port>/ "
        "Leave that server running. Do not deploy to the public internet."
    )
    return " ".join(p for p in parts if p)


def _should_ask_builder(ctx: Ctx) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    run = getattr(ctx, "run", None)
    if not run or not getattr(run, "id", None):
        return False
    mode = getattr(ctx, "mode", None) or getattr(run, "mode", None) or "guided"
    return str(mode) != "auto"


async def landing_page(ctx: Ctx) -> StepResult:
    if is_local():
        return _local_build_pause(ctx)
    from app.onboarding.gates import get_run_gate
    from app.onboarding.registry import PauseRequest
    from app.state import MEM

    repo = _repo_dir(ctx)
    company = ctx.company
    one_liner = (
        ((ctx.out.get("commit.lock_charter") or {}).get("content") or {}).get("one_liner")
        or ctx.venture.raw_idea.split(".")[0]
    )
    brief = (MEM.get(f"brief:{ctx.venture.id}") or {}).get("product") or MEM.get(
        f"product_brief:{ctx.venture.id}"
    )
    prompt = landing_builder_prompt(
        company,
        one_liner,
        ctx.venture.raw_idea or "",
        brief if isinstance(brief, dict) else None,
        waitlist_action=_interest_action(ctx),
    )
    MEM[f"builder_prompt:{ctx.venture.id}"] = prompt

    already = None
    try:
        from app.firstlook.service import landing_snapshot

        already = landing_snapshot(ctx.venture.id, _workspace_id(ctx), repo=repo)
    except Exception:
        already = None
    if already:
        MEM[f"build:{ctx.venture.id}"] = {**already, "provider": "existing"}
        return StepResult(
            output={**already, "provider": already.get("source") or "existing"},
            detail=already.get("files") or ["index.html"],
        )

    if _should_ask_builder(ctx):
        run = ctx.run
        choice = get_run_gate("builder_choice", ctx.venture.id, run.id)
        if not choice:
            return StepResult(
                output={"prompt": prompt, "awaiting": "builder_choice"},
                pause=PauseRequest(
                    reason="awaiting_builder_choice",
                    founder_action=f"/v/{ctx.venture.id}/#builder-choice",
                ),
            )
        adapter = str(choice).strip().lower()
        if adapter in {"cloud", "stub", "hosted"} and not cloud_builder_live():
            MEM[f"builder_failed:{ctx.venture.id}"] = CLOUD_UNAVAILABLE_MSG
            return StepResult(
                output={"prompt": prompt, "awaiting": "builder_choice", "error": CLOUD_UNAVAILABLE_MSG},
                pause=PauseRequest(
                    reason="awaiting_builder_choice",
                    founder_action=f"/v/{ctx.venture.id}/#builder-choice",
                ),
            )
        if adapter in {"cursor", "claude", "codex"} and not get_run_gate(
            "builder_session", ctx.venture.id, run.id
        ):
            return StepResult(
                output={"prompt": prompt, "adapter": adapter, "awaiting": "builder_session"},
                pause=PauseRequest(
                    reason="awaiting_builder_session",
                    founder_action=f"/v/{ctx.venture.id}/#builder-session",
                ),
            )
        if adapter in {"cursor", "claude", "codex"}:
            existing = repo / "index.html"
            if existing.is_file() and existing.stat().st_size > 20:
                files = [p.name for p in repo.iterdir() if p.is_file()]
                MEM[f"build:{ctx.venture.id}"] = {
                    "files": files,
                    "repo": str(repo),
                    "provider": adapter,
                }
                return StepResult(output={"files": files, "repo": str(repo), "provider": adapter}, detail=files)

    # Optional QM provider (doc 55 §6). Default path below is unchanged when unset.
    if _build_provider() == "qm":
        return await _landing_page_via_qm(ctx, repo, company, one_liner)

    # BYO coding agent path (docs 08 / 91 / 93 D4.5) — falls back to stub on any failure.
    if _build_provider() in {"byo", "cursor", "claude", "codex"}:
        return await _landing_page_via_byo(ctx, repo, company, one_liner)

    # Prefer the first-look page already rendered for this venture — it's idea-specific.
    html = ""
    fl = ctx.out.get("first_look.generate") or MEM.get(f"firstlook:{ctx.venture.id}") or {}
    fl_path = fl.get("path")
    if fl_path and Path(fl_path).is_file():
        html = Path(fl_path).read_text(encoding="utf-8")
    if not html:
        research = ctx.out.get("understand.research_market") or {}
        competitors = ", ".join(
            (c.get("name") if isinstance(c, dict) else str(c))
            for c in (research.get("competitors") or [])[:3]
        ) or "emerging alternatives"
        idea = (ctx.venture.raw_idea or one_liner).replace("<", "&lt;")
        form_html = _waitlist_form_html(ctx)
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{company}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  :root {{ --bg:#0f1419; --fg:#f4efe6; --accent:#d4a373; --muted:#a39e93; }}
  * {{ box-sizing:border-box }}
  body{{margin:0;font-family:"Iowan Old Style",Georgia,serif;background:
    radial-gradient(1200px 600px at 10% -10%,#243447 0%,transparent 55%),
    linear-gradient(165deg,var(--bg),#1b242e);color:var(--fg);min-height:100vh}}
  main{{max-width:44rem;margin:0 auto;padding:4.5rem 1.5rem 6rem}}
  .eyebrow{{letter-spacing:.14em;text-transform:uppercase;font:600 11px/1.4 system-ui,sans-serif;color:var(--accent)}}
  h1{{font-size:clamp(2rem,5vw,3rem);margin:.6rem 0 1rem;line-height:1.1}}
  .lead{{font-size:1.15rem;color:var(--muted);line-height:1.55;max-width:36rem}}
  .grid{{display:grid;gap:1rem;margin:2.5rem 0;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))}}
  .card{{border:1px solid rgba(244,239,230,.12);border-radius:14px;padding:1.1rem 1.2rem;background:rgba(255,255,255,.03)}}
  .card h3{{margin:0 0 .4rem;font-size:1rem}}
  .card p{{margin:0;color:var(--muted);font-size:.95rem;line-height:1.45}}
  form{{margin-top:2rem;display:flex;gap:.5rem;flex-wrap:wrap}}
  input,button{{font:inherit;padding:.75rem 1rem;border-radius:10px;border:0}}
  input{{flex:1;min-width:12rem;background:#fff;color:#111}}
  button{{background:var(--accent);color:#1a1208;cursor:pointer;font-weight:600}}
  .meta{{margin-top:2rem;font-size:.85rem;color:var(--muted)}}
</style></head>
<body><main>
  <div class="eyebrow">{company}</div>
  <h1>{one_liner}</h1>
  <p class="lead">{idea}</p>
  <div class="grid">
    <div class="card"><h3>Who it's for</h3><p>The first users who feel this pain most acutely, not everyone.</p></div>
    <div class="card"><h3>What ships first</h3><p>One core workflow that proves the idea, not a kitchen-sink product.</p></div>
    <div class="card"><h3>Market notes</h3><p>Watching: {competitors}.</p></div>
  </div>
{form_html}
  <p class="meta">Local first look, refine after your plan is locked.</p>
</main></body></html>
"""

    files = {
        "index.html": html,
        "package.json": json.dumps({"name": company.lower().replace(" ", "-")[:40], "private": True}, indent=2),
        "README.md": f"# {company}\n\n{one_liner}\n\nGenerated during onboarding from your idea + first look.\n",
    }
    changed = []
    if ctx.narrator:
        await ctx.narrator.code_event("read", "package.json")
        await ctx.narrator.code_event("read", "README.md")
        await ctx.narrator.code_event("edit", "index.html")
        await ctx.narrator.code_event("write", "index.html")
        await ctx.narrator.code_event("write", "package.json")
        await ctx.narrator.flush_coalesce()

    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
        changed.append(name)

    if not changed:
        from app.onboarding.failures import StepError

        raise StepError("coding_agent_no_op", "no files written")

    MEM[f"build:{ctx.venture.id}"] = {"files": changed, "repo": str(repo)}
    _queue_computer_use(ctx, company, one_liner)
    return StepResult(output={"files": changed, "repo": str(repo)}, detail=changed)


async def _landing_page_via_byo(
    ctx: Ctx, repo: Path, company: str, one_liner: str
) -> StepResult:
    """Dispatch to founder's BYO coding agent; always materialize a local preview on failure."""
    if is_local():
        return _local_build_pause(ctx)
    from coding_agents.consent import has_consent, record_consent
    from coding_agents.dispatcher import dispatch
    from coding_agents.detect import detect

    provider = _build_provider()
    prefs = (
        [provider, "cursor", "claude", "codex", "inhouse"]
        if provider in {"cursor", "claude", "codex"}
        else ["cursor", "claude", "codex", "inhouse"]
    )
    # Auto-consent inhouse for local MVP; BYO adapters still require explicit consent.
    for agent in detect():
        if not has_consent(agent.name):
            # Do not auto-consent BYO — dispatcher returns consent_required.
            pass
    if not has_consent("inhouse"):
        record_consent("inhouse", version="local")

    prompt = (
        f"Build a simple static landing page for {company}. "
        f"One-liner: {one_liner}. "
        "Produce index.html, package.json, and README.md in the working directory."
    )
    result = await dispatch(
        prompt=prompt,
        repo=repo,
        branch=f"wo/landing-{ctx.venture.id[:8]}",
        prefs=prefs,
        kind="code",
        allow_write=True,
        timeout_s=900,
        bridge_connected=True,
        mode="auto",
    )

    stub = await _stub_landing_files(ctx, repo, company, one_liner)
    stub_out = dict(stub.output or {})
    stub_out["provider"] = "byo"
    stub_out["byo_adapter"] = (result.argv[0] if result.argv else None)
    stub_out["byo_ok"] = bool(result.ok)
    stub_out["budget"] = (
        "byo_subscription" if result.ok and result.argv and result.argv[0] != "inhouse-coder" else "inhouse_or_stub"
    )
    if result.error:
        stub_out["byo_error"] = result.error
    if result.changed_files:
        stub_out["byo_changed_files"] = result.changed_files
    MEM[f"build:{ctx.venture.id}"] = stub_out
    _queue_computer_use(ctx, company, one_liner)
    return StepResult(output=stub_out, detail=stub_out.get("files") or [])


async def _landing_page_via_qm(
    ctx: Ctx, repo: Path, company: str, one_liner: str
) -> StepResult:
    """Delegate MVP landing build to QM; still ensure a local preview artifact exists."""
    if is_local():
        return _local_build_pause(ctx)
    from coding_agents import load_adapter
    from coding_agents.protocol import CodingRequest

    # Record qm_run_id on the onboarding run for cancel→abort (doc 56 QM-P3).
    os.environ["QM_BUILD_VENTURE_ID"] = ctx.venture.id
    adapter = load_adapter("qm")
    from coding_agents.gstack_charter import wrap_prompt

    prompt = wrap_prompt(
        f"Build a simple static landing page for {company}. "
        f"One-liner: {one_liner}. "
        "Produce index.html, package.json, and README.md."
    )
    result = await adapter.run(
        CodingRequest(prompt=prompt, workdir=repo, allow_write=True, timeout_s=900)
    )
    if result.session_id:
        ctx.set_flag("qm_run_id", result.session_id)

    # Always materialize a local preview so gates/publish keep working even if QM
    # sandbox files are not mirrored back into the venture repo (v1 limitation).
    stub = await _stub_landing_files(ctx, repo, company, one_liner)
    stub_out = dict(stub.output or {})
    stub_out["provider"] = "qm"
    stub_out["qm_ok"] = bool(result.ok)
    stub_out["qm_reply"] = (result.events[-1].text if result.events else None)
    if not result.ok:
        stub_out["qm_error"] = result.error
    MEM[f"build:{ctx.venture.id}"] = stub_out
    _queue_computer_use(ctx, company, one_liner)
    return StepResult(output=stub_out, detail=stub_out.get("files") or [])


async def _stub_landing_files(
    ctx: Ctx, repo: Path, company: str, one_liner: str
) -> StepResult:
    """Original stub writer extracted so QM path can share it for local preview."""
    if is_local():
        return _local_build_pause(ctx)
    # Prefer the real first-look page (working interest form) over a dead stub.
    fl = ctx.out.get("first_look.generate") or MEM.get(f"firstlook:{ctx.venture.id}") or {}
    fl_path = fl.get("path")
    if fl_path and Path(fl_path).is_file():
        html = Path(fl_path).read_text(encoding="utf-8")
    else:
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>{company}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  body{{font-family:Georgia,serif;margin:0;background:linear-gradient(160deg,#1a1a1a,#2c3e50);color:#f5f5f0;min-height:100vh}}
  main{{max-width:40rem;margin:0 auto;padding:4rem 1.5rem}}
  h1{{font-size:2.4rem;margin:0 0 .5rem}}
  p{{opacity:.85;line-height:1.5}}
  form{{margin-top:2rem;display:flex;gap:.5rem;flex-wrap:wrap}}
  input,button{{font:inherit;padding:.6rem .9rem;border-radius:4px;border:0}}
  input{{flex:1;min-width:12rem}}
  button{{background:#e8c547;color:#111;cursor:pointer}}
</style></head>
<body><main>
  <h1>{company}</h1>
  <p>{one_liner}</p>
{_waitlist_form_html(ctx)}
</main></body></html>
"""
    files = {
        "index.html": html,
        "package.json": json.dumps(
            {"name": company.lower().replace(" ", "-")[:40], "private": True}, indent=2
        ),
        "README.md": f"# {company}\n\nGenerated during onboarding.\n",
    }
    changed = []
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
        changed.append(name)
    return StepResult(output={"files": changed, "repo": str(repo)}, detail=changed)


async def run_gates(ctx: Ctx) -> StepResult:
    build = ctx.out.get("build.landing_page") or {}
    files = build.get("files") or []
    gate_total = 12
    # Stub: pass all if landing exists
    gate_pass = gate_total if files else 0
    return StepResult(
        output={"gate_pass": gate_pass, "gate_total": gate_total},
        tokens={"gate_pass": gate_pass, "gate_total": gate_total},
        partial=gate_pass < gate_total,
    )


async def publish(ctx: Ctx) -> StepResult:
    import os

    host = ctx.out.get("provision.hosting") or {}
    # Prefer the first-look HTTP route that Company OS actually serves.
    wid = getattr(ctx.venture, "workspace_id", None) or getattr(ctx.run, "workspace_id", None)
    if wid:
        core = os.environ.get("CORE_PORT") or "3300"
        url = f"http://127.0.0.1:{core}/w/{wid}/site/"
    else:
        url = host.get("url") or f"http://127.0.0.1:4173"
    # L1/L2 → preview only
    preview = ctx.downgraded or (ctx.venture.autonomy_level or "L1") in {"L1", "L2"}
    MEM[f"website:{ctx.venture.id}"] = {
        "url": url,
        "published": not preview,
        "simulated": True,
        "provider": host.get("provider") or "local",
    }
    return StepResult(
        output={"url": url, "published": not preview, "preview": preview},
        tokens={"url": url},
        downgraded=preview,
        simulated=True,
    )


async def close_sandbox(ctx: Ctx) -> StepResult:
    return StepResult(output={"closed": True})

from fastapi import Body, HTTPException
from app.db.models import Venture
from app.onboarding.gates import get_run_gate, set_run_gate
from app.onboarding.state import active_run, resumable_run

LOCAL_ADAPTERS = {'cursor', 'claude', 'codex', 'opencode'}

CLOUD_ADAPTERS = {'cloud', 'stub', 'hosted'}

def builder_venture(venture_id: str) -> Venture:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        session.expunge(ven)
        return ven

def builder_prompt_for(ven: Venture) -> str:
    brief = (MEM.get(f"brief:{ven.id}") or {}).get("product") or MEM.get(f"product_brief:{ven.id}")
    wid = getattr(ven, "workspace_id", None)
    from app.firstlook.waitlist import public_interest_url

    action = public_interest_url(wid) if wid else "#"
    prompt = landing_builder_prompt(
        getattr(ven, "name", "") or "this venture",
        getattr(ven, "one_liner", "") or "",
        getattr(ven, "raw_idea", "") or "",
        brief if isinstance(brief, dict) else None,
        waitlist_action=action,
    )
    if is_local():
        from app.secrets.detect import looks_like_secret

        if looks_like_secret(prompt):
            raise HTTPException(409, detail={"code": "build_request_contains_secret", "message":
                "Remove credentials from your idea and brief. Save keys in Settings > Keys and connections, then retry your page."})
        prompt += " Use a light theme. Keep private notes and checks under review/. Do not purchase services or publish this page."
    MEM[f"builder_prompt:{ven.id}"] = prompt
    return prompt

def builder_choice_payload(venture_id: str) -> dict:
    ven = builder_venture(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    adapter = get_run_gate("builder_choice", venture_id, run.id) if run else MEM.get(f"builder_choice:{venture_id}")
    session = get_run_gate("builder_session", venture_id, run.id) if run else MEM.get(f"builder_session:{venture_id}")
    failed = MEM.get(f"builder_failed:{venture_id}")
    live = False if is_local() else cloud_builder_live()
    payload = {
        "adapter": adapter,
        "session": session,
        "prompt": builder_prompt_for(ven),
        "paused_reason": getattr(run, "paused_reason", None) if run else None,
        "error": str(failed) if failed else None,
        "cloud_available": live,
    }
    if is_local():
        order = _local_builder_order(venture_id, run)
        if order:
            payload.update(device_work_order_id=order["id"], review_path=order["review_path"])
    return payload

def builder_get_builder_choice(venture_id: str):
    return builder_choice_payload(venture_id)

async def builder_choose_builder(venture_id: str, payload):
    builder_venture(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    if not run:
        raise HTTPException(409, {"code": "no_active_run", "message":
            "Open your venture chat and ask to build your landing page, then choose your connected coding tool."} if is_local() else "no_active_run")
    adapter = payload.adapter
    if is_local():
        if adapter not in LOCAL_ADAPTERS:
            raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message":
                "Connect your own coding tool in Settings > Keys and connections > Coding tools, then choose it to build your page."})
        result = _local_build_pause(Ctx(venture=builder_venture(venture_id), run=run), provider=adapter)
        if not result.output.get("device_work_order_id"):
            raise HTTPException(409, detail={"code": "builder_needs_setup", "message": result.output["message"]})
        set_run_gate("builder_choice", venture_id, run.id, adapter)
        return {**builder_choice_payload(venture_id), **result.output, "status": "queued"}
    if adapter in CLOUD_ADAPTERS and not cloud_builder_live():
        MEM[f"builder_failed:{venture_id}"] = CLOUD_UNAVAILABLE_MSG
        raise HTTPException(
            status_code=409,
            detail={"error": "cloud_builder_unavailable", "message": CLOUD_UNAVAILABLE_MSG},
        )
    set_run_gate("builder_choice", venture_id, run.id, adapter)
    MEM.pop(f"builder_failed:{venture_id}", None)
    MEM[f"builder_prompt:{venture_id}"] = builder_prompt_for(builder_venture(venture_id))
    if adapter in CLOUD_ADAPTERS:
        set_run_gate("builder_session", venture_id, run.id, "cloud")
    should_resume = run.status == "paused" and run.paused_reason in {
        "awaiting_builder_choice",
        "awaiting_builder_session",
    }
    if should_resume:
        from app.onboarding.spawn import start_onboarding

        await start_onboarding(
            venture_id,
            mode=run.mode,
            resume=True,
            run_id=run.id,
            entry_mode=getattr(run, "entry_mode", None) or "idea",
            workspace_id=getattr(run, "workspace_id", None),
        )
    out = builder_choice_payload(venture_id)
    out["status"] = "start_local" if adapter in LOCAL_ADAPTERS else "resumed"
    return out

async def builder_fail_builder_session(venture_id: str, payload):
    builder_venture(venture_id)
    MEM[f"builder_failed:{venture_id}"] = payload.text
    return {"status": "failed", **builder_choice_payload(venture_id)}

async def builder_finish_builder_session(
    venture_id: str, payload = Body(default=None)
):
    builder_venture(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    if not run:
        raise HTTPException(409, {"code": "no_active_run", "message":
            "Open Builds in your venture and start your requested landing page before importing its result."} if is_local() else "no_active_run")
    local = is_local()
    if local:
        if not _local_order_verified(_local_builder_order(venture_id, run)):
            raise HTTPException(409, detail={"code": "builder_not_verified", "message":
                "Open Builds and wait for your coding tool to finish its checks, then import its page. Nothing is marked complete."})
        if get_run_gate("builder_session", venture_id, run.id) == "done":
            return {"status": "accepted", **builder_choice_payload(venture_id)}
    MEM.pop(f"builder_failed:{venture_id}", None)
    if not local and not get_run_gate("builder_choice", venture_id, run.id):
        set_run_gate("builder_choice", venture_id, run.id, "cursor")
    already = get_run_gate("builder_session", venture_id, run.id)
    ingested = None
    html = (payload.html if payload else None) or ((MEM.get(f"page_files:{venture_id}") or {}).get("index.html") if local else "") or ""
    if local and (not html.strip() or (payload is not None and not payload.ready)):
        raise HTTPException(409, detail={"code": "builder_source_required", "message":
            "Open Builds and import the checked index.html from your coding tool before finishing your page."})
    # Full HTML in this request is often blocked by WAF (8KB body / XSS). Prefer /builder/page chunks.
    if html.strip() and (local or len(html) < 4000):
        try:
            from app.firstlook.service import ingest_builder_page

            extras = {k: v for k, v in ((payload.files if payload else None) or
                      (MEM.get(f"page_files:{venture_id}") if local else None) or {}).items()
                      if isinstance(v, str) and (not local or k != "index.html")}
            ingested = ingest_builder_page(venture_id, html, extras)
        except Exception:  # noqa: BLE001
            ingested = {"ok": False, "error": "page_ingest_failed",
                        "message": "The page could not be saved. Please try again."}
    if local and (ingested or {}).get("ok") is not True:
        raise HTTPException(409, detail={"code": "page_ingest_failed", "message":
            "Open Builds and import your coding tool's checked page again. Your page could not be saved, so it is not marked complete."})
    if local:
        adapter = _local_builder_order(venture_id, run)["manifest"]["coding_verification"]["provider"]
        set_run_gate("builder_choice", venture_id, run.id, adapter)
    set_run_gate("builder_session", venture_id, run.id, "done")
    should_resume = (
        already != "done"
        and run.status == "paused"
        and run.paused_reason in {"awaiting_builder_session", "awaiting_builder_choice"}
    )
    if should_resume:
        from app.onboarding.spawn import start_onboarding

        await start_onboarding(
            venture_id,
            mode=run.mode,
            resume=True,
            run_id=run.id,
            entry_mode=getattr(run, "entry_mode", None) or "idea",
            workspace_id=getattr(run, "workspace_id", None),
            background=True,
        )
    out = {
        "status": "resumed" if should_resume else "accepted",
        **builder_choice_payload(venture_id),
    }
    if ingested:
        out["site"] = ingested
    return out

async def builder_receive_builder_page_chunk(venture_id: str, payload):
    """Accept a WAF-safe (base64, <8KB) slice of a local landing file, then ingest."""
    builder_venture(venture_id)
    name = payload.name.strip()
    if "/" in name or name.startswith(".") or name not in {"index.html", "styles.css", "style.css", "main.css"}:
        raise HTTPException(400, {"code": "bad_name", "message":
            "Open Builds and import index.html or a stylesheet from your coding tool's page, without folders."} if is_local() else "bad_name")
    try:
        import base64

        piece = base64.b64decode(payload.b64.encode("ascii"), validate=True).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, {"code": "bad_chunk", "message":
            "Open Builds and import your coding tool's page again. This file's transfer could not be read."} if is_local() else "bad_chunk") from exc
    key = f"page_parts:{venture_id}:{name}"
    parts: dict[int, str] = dict(MEM.get(key) or {})
    parts[int(payload.index)] = piece
    MEM[key] = parts
    if len(parts) < int(payload.total):
        return {"status": "chunk", "got": len(parts), "total": payload.total, "name": name}
    ordered = [parts[i] for i in range(payload.total) if i in parts]
    if len(ordered) != payload.total:
        return {"status": "chunk", "got": len(parts), "total": payload.total, "name": name}
    MEM.pop(key, None)
    files: dict[str, str] = dict(MEM.get(f"page_files:{venture_id}") or {})
    files[name] = "".join(ordered)
    MEM[f"page_files:{venture_id}"] = files
    html = files.get("index.html") or ""
    if len(html.strip()) < 40:
        return {"status": "stored", "name": name}
    extras = {k: v for k, v in files.items() if k != "index.html"}
    try:
        from app.firstlook.service import ingest_builder_page

        ingested = ingest_builder_page(venture_id, html, extras)
    except Exception:  # noqa: BLE001
        ingested = {"ok": False, "error": "page_ingest_failed",
                    "message": "The page could not be saved. Please try again."}
    if is_local() and ingested.get("ok") is not True:
        ingested["message"] = "Open Builds and import your coding tool's checked page again. Your page could not be saved."
        return {"status": "failed", "name": name, "site": ingested}
    return {"status": "ingested", "name": name, "site": ingested}

async def builder_builder_progress(venture_id: str, payload):
    from core.copy import strip_emdash
    from app.onboarding.narrator import Narrator

    builder_venture(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    narrator = Narrator(venture_id, getattr(run, "id", None) if run else None)
    text = strip_emdash(payload.text)
    if "wrote" in text.lower() or "looks ready" in text.lower():
        MEM.pop(f"builder_failed:{venture_id}", None)
    await narrator.emit(
        phase="build",
        kind="step",
        status="running",
        text=text,
        step_id="build.landing_page",
    )
    return {"status": "ok", "text": text}

async def builder_complete_builder_because_page_exists(venture_id: str, run) -> bool:
    """Skip the builder picker when a hosted page is already in Postgres."""
    from app.firstlook.service import landing_exists

    if is_local() and (get_run_gate("builder_session", venture_id, run.id) != "done"
                       or not _local_order_verified(_local_builder_order(venture_id, run))):
        return False
    if not landing_exists(venture_id, getattr(run, "workspace_id", None)):
        return False
    if run.status != "paused" or run.paused_reason not in {
        "awaiting_builder_choice",
        "awaiting_builder_session",
    }:
        return False
    if not get_run_gate("builder_choice", venture_id, run.id):
        set_run_gate("builder_choice", venture_id, run.id, "cursor")
    set_run_gate("builder_session", venture_id, run.id, "done")
    MEM.pop(f"builder_failed:{venture_id}", None)
    from app.onboarding.spawn import start_onboarding

    await start_onboarding(
        venture_id,
        mode=run.mode,
        resume=True,
        run_id=run.id,
        entry_mode=getattr(run, "entry_mode", None) or "idea",
        workspace_id=getattr(run, "workspace_id", None),
        background=True,
    )
    return True

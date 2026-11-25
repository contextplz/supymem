"""Host my product: choices, guided deploy steps, and Core's own check of the live URL.

The reviewed coding sandbox disables network and hides the founder's home folder, so a coding order can
prepare a deploy-ready project but cannot log in to or push to a host. The founder runs one command in
Terminal (guided here, step by step). Core then fetches the address itself; only that fetch can mark the
hosted product "verified". Secret values never enter chat: the backend form is the existing secret slot.
"""
from __future__ import annotations

import json
import re

from app.chat import service_guides as sg
from app.chat.actions import question_reply
from app.core.local_mode import is_local
from app.db.models import Venture, VentureDocument
from app.state import db
from core.clock import utcnow
from core.research_web.guard import get_checked

ACT_CHOOSE, ACT_PLAN, ACT_VERIFY = "host.choose", "host.plan", "host.verify"
# host -> (one-line trade-off, login command, deploy command, where the production URL appears)
HOSTS = {
    "vercel": ("Best for web apps (Next.js and most frameworks). Free Hobby plan is personal/non-commercial only.",
               "npx vercel login", "npx vercel --prod --yes"),
    "netlify": ("Best for static sites and simple front ends; allowed for commercial use on Free, with a monthly limit.",
                "npx netlify-cli login", "npx netlify-cli deploy --prod --dir=. --create-site"),
    "cloudflare": ("Best for static sites; generous free limits and fast worldwide.",
                   "npx wrangler login", "npx wrangler pages deploy . --project-name=YOUR-PROJECT-NAME"),
    "render": ("For apps that need a server process; Free sleeps when idle, so it is for previews.", "", ""),
}
RECOMMENDED = {"hosting": "vercel", "backend": "supabase"}
BACKENDS = ("supabase", "neon", "firebase")
ERROR_TITLE = re.compile(r"(?i)\b(404|403|500|502|503|not found|error|forbidden|unavailable|suspended|"
                         r"deployment (?:not found|has been)|page isn.t working|application error|log in|sign in to)")
ERROR_BODY = re.compile(r"(?i)DEPLOYMENT_NOT_FOUND|The deployment could not be found|Site not found|"
                        r"Application error: a server-side exception|Authentication Required|"
                        r"There isn.t a GitHub Pages site here|This Account has been suspended")
MAX_HOPS, MAX_BYTES = 3, 400_000


def _reply(body: str, actions: list | None = None) -> dict:
    return {"kind": "text", "body_md": body, "actions": actions or [], "citations": []}


def _emit(name: str, venture_id: str, props: dict) -> None:
    try:
        from app.analytics.events import emit
        from app.chat.recovery import _workspace

        emit(name, person_id=None, workspace_id=_workspace(venture_id), venture_id=venture_id,
             props={"feature": "hosting", **props})
    except Exception as exc:  # noqa: BLE001 - analytics never blocks the founder
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "hosting.analytics")


def matches(text: str) -> bool:
    return bool(re.search(
        r"(?i)^\W*(?:please\s+|can you\s+|i want to\s+|let'?s\s+)?(?:host (?:my|the|this) (?:product|app|site|website)|"
        r"host it\b|deploy (?:it|my (?:product|app|site)|the app)\b|put (?:it|my (?:product|app|site)) online|"
        r"make (?:it|my (?:product|app)) (?:live|public)|go live with my (?:product|app)|verify my (?:hosted )?product)", text))


# ------------------------------------------------------------------ choices
def _local_plan() -> dict:
    from app.chat.actions import beta_blocked
    from app.core.local_secrets import read_settings

    actions = [{"label": "Build your landing page", "action": "pagebuilder.build", "style": "primary"}]
    body = "You can build a landing page and export it from Website to publish yourself."
    blocked = beta_blocked("site.publish")
    if blocked:
        body += "\n\n" + blocked["body_md"]
        actions.append({"label": "Open Keys and connections", "href": "/settings/local", "style": "secondary"})
    else:
        provider = read_settings("publishing")["values"].get("provider")
        label = {"vercel": "Vercel", "netlify": "Netlify", "s3": "S3"}.get(provider, "your publishing service")
        body += f"\n\nYou can publish your page with {label} using your saved settings. Choose Publish your page to put it online."
        actions.append({"label": "Publish your page", "action": "site.publish", "style": "primary"})
    return question_reply(body, actions)


def start(venture_id: str) -> dict:
    if is_local():
        return _local_plan()
    with db()() as session:
        done = _hosted(session, venture_id)
    if done.get("verified"):
        return status(venture_id)
    actions = [{"label": f"{sg.PROVIDERS[h][1]}{' (recommended)' if h == RECOMMENDED['hosting'] else ''}",
                "action": ACT_CHOOSE, "style": "primary" if h == RECOMMENDED["hosting"] else "secondary",
                "payload": {"category": "hosting", "provider": h}} for h in HOSTS]
    lines = "\n".join(f"- **{sg.PROVIDERS[h][1]}**: {HOSTS[h][0]}" for h in HOSTS)
    _emit("hosting.started", venture_id, {})
    return question_reply("Let's put your product online for free. First, where should it live?\n\n" + lines +
                  "\n\nI recommend Vercel for a web app, or Netlify / Cloudflare Pages for a static site. Pick one below; you can change it later.",
                  actions)


def choose(venture_id: str, payload: dict) -> dict:
    category, provider = payload.get("category"), payload.get("provider")
    if set(payload) - {"category", "provider"} or category not in {"hosting", "backend"}:
        return _reply("Choose one of the buttons. Nothing was saved.")
    if category == "hosting" and provider not in HOSTS:
        return _reply("Choose one of the hosting buttons. Nothing was saved.")
    if category == "backend" and provider not in (*BACKENDS, "none"):
        return _reply("Choose one of the backend buttons. Nothing was saved.")
    if is_local():
        return _local_plan()
    with db()() as session:
        if session.get(Venture, venture_id, with_for_update=True) is None:
            raise KeyError("venture_not_found")
        values = sg._load(session, venture_id)
        if provider == "none":
            values.pop("backend", None)
        else:
            values[category] = provider
        sg._save(session, venture_id, "service_plan", values)
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief

    invalidate_venture_state_brief(venture_id)
    _emit("hosting.choice_saved", venture_id, {"category": category, "provider": provider})
    if category == "hosting":
        actions = [{"label": f"{sg.PROVIDERS[b][1]}{' (recommended)' if b == RECOMMENDED['backend'] else ''}",
                    "action": ACT_CHOOSE, "style": "primary" if b == RECOMMENDED["backend"] else "secondary",
                    "payload": {"category": "backend", "provider": b}} for b in BACKENDS]
        actions.append({"label": "My product needs no backend", "action": ACT_CHOOSE, "style": "ghost",
                        "payload": {"category": "backend", "provider": "none"}})
        return question_reply(f"{sg.PROVIDERS[provider][1]} it is. Does your product need to remember users' data or let people sign in?\n\n"
                      + "\n".join(f"- **{sg.PROVIDERS[b][1]}**: {sg.PROVIDERS[b][2]}" for b in BACKENDS)
                      + "\n\nI recommend Supabase: database, sign-in and file storage together, free to start.", actions)
    return plan(venture_id)


# ------------------------------------------------------------------ guided deploy
def _workspace_path(venture_id: str) -> str:
    from app.build.device_orders import _workspace_slug

    with db()() as session:
        venture = session.get(Venture, venture_id)
    return f"~/Documents/Supymem/ventures/{_workspace_slug(venture.name if venture else '', venture_id)}"


def _prepare_goal(host: str, backend: str | None) -> str:
    cfg = {"vercel": "no host config file is needed", "netlify": "add a netlify.toml with the build command and publish folder",
           "cloudflare": "add the build output folder to DEPLOY.md", "render": "add a render.yaml"}[host]
    names = " Read the backend variable names from .env.example only." if backend else ""
    return (f"Build with {{provider}}: make this project ready to deploy on {sg.PROVIDERS[host][1]}. Make sure the "
            f"workspace is a git repository with one commit, {cfg}, add a .env.example listing variable NAMES with empty values, "
            f"and write DEPLOY.md with the exact production build command and output folder.{names} "
            "Do not deploy, sign in or create accounts.")


def plan(venture_id: str) -> dict:
    if is_local():
        return _local_plan()
    with db()() as session:
        choices = sg._load(session, venture_id)
    host, backend = choices.get("hosting"), choices.get("backend")
    if host not in HOSTS:
        return start(venture_id)
    name, folder = sg.PROVIDERS[host][1], _workspace_path(venture_id)
    steps, actions = [], []
    if backend == "supabase":
        from app.secrets.need import is_armed, request_for_provider

        if is_armed("supabase.project"):
            steps.append("Supabase keys are saved. I never show them.")
        else:
            opened = request_for_provider("supabase", venture_id=venture_id,
                                          reason="Connect your Supabase project to your hosted product.")
            steps.append("Backend: go to https://supabase.com/dashboard, click **Continue with GitHub** or **Sign in**, then **New project** (Free). "
                         "When it is ready click **Connect**, copy the **Project URL** and the **anon / publishable key**, and paste both into the secure form below. Never paste them in chat.")
            actions.append({"label": "Enter Supabase keys", "kind": "secret_form", "style": "primary",
                            "slot_id": (opened or {}).get("slot_id") or "supabase.project"})
    elif backend:
        steps.append(f"Backend: follow the {sg.PROVIDERS[backend][1]} guide (button below), then come back.")
        actions.append({"label": f"{sg.PROVIDERS[backend][1]} setup steps", "action": "service.guide", "style": "secondary",
                        "payload": {"provider": backend}})
    for provider in ("claude", "codex"):
        goal = _prepare_goal(host, backend).format(provider=provider.title())
        actions.append({"label": f"Prepare it with {provider.title()}", "action": "mac.task.prepare", "style": "secondary",
                        "text": goal, "payload": {"text": goal}})
    if HOSTS[host][2]:
        from app.needs_you.hooks import hosting_login

        hosting_login(venture_id, host)  # only the founder can sign in to the host
        _, login, deploy = HOSTS[host]
        steps += [f"Prepare: tap **Prepare it with Claude** (or Codex) below and approve the order. Your coding agent makes the project deploy-ready in `{folder}`.",
                  "Open the **Terminal** app on your Mac (press Cmd+Space, type Terminal, press Return).",
                  f"Paste `cd {folder}` and press Return.",
                  f"Paste `{login}` and press Return. Your browser opens: click **Continue with Google** and choose your own account, then allow.",
                  f"Paste `{deploy}` and press Return. Answer the prompts with the default (press Return).",
                  "When it finishes it prints a **Production** web address. Copy it."]
    else:
        steps += [f"Follow the {name} steps (button below).", "Copy the public web address when it is live."]
        actions.append({"label": f"{name} setup steps", "action": "service.guide", "style": "secondary",
                        "payload": {"provider": host}})
    steps.append("Paste it here as: **verify my product https://YOUR-ADDRESS**. I will open it myself and check it.")
    _emit("hosting.plan_shown", venture_id, {"host": host, "backend": backend or "none"})
    return _reply(f"Here is everything, one step at a time (free {name}):\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), actions)


# ------------------------------------------------------------------ verification
def _get(url: str) -> tuple[int, str, str]:
    """(status, final_url, body). Redirects are followed by hand so each hop is re-checked as public https."""
    import httpx

    with httpx.Client(follow_redirects=False, trust_env=False, timeout=10,
                      headers={"User-Agent": "SupymemVerify/1"}) as client:
        response = get_checked(client, url, max_redirects=MAX_HOPS, https_only=True)
    return response.status_code, response.extensions.get("logical_url", url), response.text[:MAX_BYTES]


def check(url: str) -> dict:
    """Core's own look at the address: HTTP 200, a real title, no host error page."""
    try:
        code, final, body = _get(url)
    except Exception as error:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(error, None, "hosting.check")
        return {"ok": False, "reason": "I could not open that address to check it. Please try again."}
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    title = re.sub(r"\s+", " ", match.group(1)).strip()[:120] if match else ""
    if code != 200:
        return {"ok": False, "reason": f"The address answered with HTTP {code}, not 200."}
    if ERROR_BODY.search(body) or (title and ERROR_TITLE.search(title)):
        return {"ok": False, "reason": f"The page looks like an error or login page (title: \"{title or 'none'}\")."}
    if not title:
        return {"ok": False, "reason": "The page loaded but has no title, so I cannot tell it is your product."}
    return {"ok": True, "title": title, "url": final}


def _hosted(session, venture_id: str) -> dict:
    row = session.get(VentureDocument, f"hosted-product:{venture_id}")
    if row is None or row.venture_id != venture_id:
        return {}
    try:
        return json.loads(row.body_md or "{}")
    except (TypeError, ValueError):
        return {}


def verify(venture_id: str, url: object, *, founder_provided: bool = True) -> dict:
    """Check the address, then save one hosted-product record: verified only if Core's fetch passed."""
    clean = sg._public_url(url)
    if not clean:
        return _reply("Send only a public https address without a query string: **verify my product https://YOUR-ADDRESS**. Nothing was saved.")
    result = check(clean)
    with db()() as session:
        if session.get(Venture, venture_id, with_for_update=True) is None:
            raise KeyError("venture_not_found")
        choices = sg._load(session, venture_id)
        values = {"url": clean, "host": choices.get("hosting"), "services": choices,
                  "state": "verified" if result["ok"] else "founder_reported", "verified": result["ok"],
                  "source": "founder_provided" if founder_provided else "supymem_deploy"}
        if result["ok"]:
            values.update(title=result["title"], verified_at=utcnow().isoformat())
        sg._save(session, venture_id, "hosted_product", values, path=clean,
                 provenance={"source": values["source"], "verified": result["ok"],
                             "verified_by": "core_http_fetch" if result["ok"] else None})
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief

    invalidate_venture_state_brief(venture_id)
    _emit("hosting.verify_passed" if result["ok"] else "hosting.verify_failed", venture_id,
          {"host": values["host"] or "unknown", "has_reason": not result["ok"]})
    open_btn = {"label": "Open product", "href": clean, "style": "primary"}
    if result["ok"]:
        from app.needs_you.service import resolve_ref

        resolve_ref(venture_id, f"host:{values['host']}")  # the login step is evidently done
        from app.chat.handlers.general import _next_step_actions, _next_step_line

        line = _next_step_line(venture_id, None)
        return _reply(f"Your product is live and I checked it myself: HTTP 200, page title \"{result['title']}\". "
                      "I recorded it as your hosted product. I did not test sign-in or saved data, so try one real flow yourself. "
                      + (f"Next: {line}" if line else ""), [open_btn, *_next_step_actions(venture_id)])
    from app.chat import recovery

    return _reply(f"I saved your address as founder-reported, but it is not verified. {result['reason']} "
                  "Fix it and send the address again, or take the manual route.",
                  [open_btn, recovery.button("host.deploy", kind="host_failed", url=clean)])


def status(venture_id: str) -> dict:
    with db()() as session:
        hosted = _hosted(session, venture_id)
    if not hosted.get("url"):
        return start(venture_id)
    state = (f"verified by Supymem on {hosted['verified_at'][:10]}, page title \"{hosted.get('title', '')}\""
             if hosted.get("verified") else "founder-reported, not verified")
    return _reply(f"Your hosted product is {hosted['url']} ({state}).",
                  [{"label": "Open product", "href": hosted["url"], "style": "primary"},
                   {"label": "Check it again", "action": ACT_VERIFY, "style": "secondary", "payload": {"url": hosted["url"]}}])


def from_text(venture_id: str, text: str) -> dict:
    found = re.search(r"https?://\S+", text)
    if found and re.search(r"(?i)\bverify\b", text):
        return verify(venture_id, found[0].rstrip(".,)"))
    return start(venture_id)


# Charlie attaches this one-tap offer when a coding build finishes.
OFFER = {"label": "Host my product", "action": ACT_PLAN, "style": "secondary", "payload": {}}

"""Concrete service setup instructions; choices/URLs are saved, provisioning is never claimed.

Only provider names and a public product URL enter these records. Credentials
stay in the founder's local ignored env file or their host's environment form.
Vendor instructions checked against official docs on 2026-10-02.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from core.clock import utcnow
from app.chat.actions import question_reply
from app.db.models import Venture, VentureDocument
from app.state import db

PROVIDERS = {
    "netlify": ("hosting", "Netlify", "Web apps and static sites; Free has a hard monthly usage limit.",
                "https://app.netlify.com/", "https://docs.netlify.com/start/quickstarts/deploy-from-repository/"),
    "render": ("hosting", "Render", "Server apps; Free sleeps when idle and is for previews, not reliable production.",
               "https://dashboard.render.com/", "https://render.com/docs/your-first-deploy"),
    "vercel": ("hosting", "Vercel", "Next.js; free Hobby is only for personal, non-commercial projects.",
               "https://vercel.com/new", "https://vercel.com/docs/plans/hobby"),
    "cloudflare": ("hosting", "Cloudflare Pages", "Static sites and front ends; generous free limits, fast worldwide.",
                   "https://dash.cloudflare.com/", "https://developers.cloudflare.com/pages/get-started/"),
    "firebase": ("backend", "Firebase", "Google's database, sign-in and file storage; free Spark plan, but it ties you to Google's tools.",
                 "https://console.firebase.google.com/", "https://firebase.google.com/docs/web/setup"),
    "supabase": ("backend", "Supabase", "Database, sign-in and file storage together; recommended starting point.",
                 "https://supabase.com/dashboard", "https://supabase.com/docs/guides/getting-started/api-keys"),
    "neon": ("backend", "Neon", "PostgreSQL database; sign-in and file storage need separate services.",
             "https://console.neon.tech/", "https://neon.com/docs/get-started-with-neon/signing-up"),
}
GUIDE_STEPS = {
    "netlify": [
        "Open the Netlify dashboard and sign in. Keep the Free plan for this first experiment.",
        "Click Add new project, then Import an existing project. Choose your Git provider, authorize the app repository, and select it.",
        "Use the build command and output folder from your coding agent's DEPLOY.md. Add the env names listed there through the project's Environment variables form; paste their values there, never here.",
        "Click Publish. Check Deploys for a successful build. Check project visibility is public, then open its netlify.app address in a signed-out browser.",
        "Complete one real flow: sign in, save an item, reload and see it again. Return here with only: Record my hosted product https://YOUR-PUBLIC-ADDRESS.",
    ],
    "render": [
        "Open the Render dashboard and sign in. Click + New, then Web Service for a server app or Static Site for static files.",
        "Connect your Git provider, select the app repository and click Connect. Set the root folder, build command and start command from the coding agent's DEPLOY.md.",
        "For a web service, select Free under Compute. Paste credentials into Environment Variables only. The server must listen on 0.0.0.0 and the PORT supplied by Render.",
        "Click Deploy. In Deploys, wait for Live; open the onrender.com URL and exercise one saved-data flow. Free services sleep after 15 idle minutes; local files are temporary. Use an external database, not local SQLite. Free Render Postgres expires after 30 days.",
        "Return here with only: Record my hosted product https://YOUR-PUBLIC-ADDRESS. This records your report; it does not verify the deployment.",
    ],
    "vercel": [
        "Use Hobby only for a personal, non-commercial project. For a startup, choose Netlify or another eligible plan before deploying.",
        "Open Vercel New Project, connect your Git provider and import the app repository.",
        "Check the detected framework, root folder and build command against DEPLOY.md. In Environment Variables, enter the listed names and paste values there, never in chat.",
        "Click Deploy. After it completes, open the production URL in a signed-out browser and test sign-in plus a save-and-reload flow.",
        "Return here with only: Record my hosted product https://YOUR-PUBLIC-ADDRESS. A public URL alone does not prove the app works.",
    ],
    "cloudflare": [
        "Open the Cloudflare dashboard and sign in (or create a free account). Go to Workers & Pages, click Create, then the Pages tab.",
        "Choose Connect to Git and import your app repository, or Upload assets for a ready-made folder.",
        "Use the build command and output folder from DEPLOY.md. Add any environment variables there; paste values in that form, never in chat.",
        "Click Save and Deploy. Open the pages.dev address in a signed-out browser.",
        "Return here with only: verify my product https://YOUR-PUBLIC-ADDRESS.",
    ],
    "firebase": [
        "Open the Firebase console and sign in with Google. Click Add project, name it, and skip Google Analytics.",
        "Click the web icon (</>) to register a web app and copy its config values into your app's ignored .env.local using the names in .env.example.",
        "In Build, enable Authentication (the sign-in methods you need) and Firestore Database in production mode with security rules your coding agent writes.",
        "Test a sign-in plus a save-and-reload flow. Do not paste config values or any service-account key in chat.",
        "Return here with 'Firebase setup ready' and the step you tested.",
    ],
    "supabase": [
        "Open the Supabase dashboard and sign in. Choose your organization, click New project, choose Free if available, name the project and select a nearby region. Store its database password in your password manager.",
        "Wait for the project to be ready. Open Connect and choose your framework. Copy the project URL and publishable key into your app's ignored .env.local file using the names in .env.example.",
        "For Next.js, those names are NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY. For other frameworks, use the names your coding agent supplies. Do not paste values into this chat.",
        "If server-only admin work needs a secret key, get it in Settings > API Keys and set SUPABASE_SECRET_KEY in your host's private environment form. Never give it a NEXT_PUBLIC_ prefix.",
        "Ask the coding agent for a reviewed migration and row-level security policies. In SQL Editor, paste that migration and click Run. Test that one signed-in account cannot read another's records.",
        "Return here with 'Supabase setup ready' and the step you tested. This is your report, not an automatic connection check.",
    ],
    "neon": [
        "Open the Neon console and sign in. Create a project, choose the free option shown, name it and choose a region near the app.",
        "On Project Dashboard, click Connect, choose the branch and database, and copy its PostgreSQL connection string into DATABASE_URL in your app's ignored local env file and host's private Environment Variables form. It contains a password; never paste it in chat or browser code.",
        "Ask the coding agent for the app migration and a server-only database adapter. Run the reviewed migration using its documented command.",
        "Test a save-and-reload flow through your app. Add a separate sign-in or file storage provider if needed; Neon database setup alone does not provide those capabilities.",
        "Return here with 'Neon setup ready' and what you tested, without the connection string.",
    ],
}


def _reply(body: str, actions: list | None = None) -> dict:
    return {"kind": "text", "body_md": body, "actions": actions or [], "citations": []}


def _load(session, venture_id: str) -> dict:
    row = session.get(VentureDocument, f"service-plan:{venture_id}")
    if row is None or row.venture_id != venture_id:
        return {}
    try:
        values = json.loads(row.body_md or "{}")
    except (ValueError, TypeError):
        return {}
    return {category: provider for category, provider in values.items()
            if category in {"hosting", "backend"} and isinstance(provider, str) and provider in PROVIDERS
            and PROVIDERS[provider][0] == category} if isinstance(values, dict) else {}


def _save(session, venture_id: str, kind: str, values: dict, path: str | None = None, provenance: dict | None = None) -> None:
    venture = session.get(Venture, venture_id)
    if venture is None:
        raise KeyError("venture_not_found")
    doc_id = f"{kind.replace('_', '-')}:{venture_id}"
    row = session.get(VentureDocument, doc_id)
    if row is not None and row.venture_id != venture_id:
        raise ValueError("service_record_scope_mismatch")
    if row is None:
        row = VentureDocument(id=doc_id, venture_id=venture_id, kind=kind,
                              title="Product services" if kind == "service_plan" else "Founder-reported product",
                              created_at=utcnow(), updated_at=utcnow())
        session.add(row)
    row.body_md = json.dumps(values, sort_keys=True)
    row.path, row.updated_at = path, utcnow()
    row.provenance_json = json.dumps(provenance or {"source": "founder_provided", "verified": False})


def guide(venture_id: str, provider: str = "", category: str = "") -> dict:
    if isinstance(provider, str) and provider in PROVIDERS:
        _, name, tradeoff, dashboard, docs = PROVIDERS[provider]
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(GUIDE_STEPS[provider], 1))
        return _reply(f"{name}: {tradeoff}\n\n{steps}\n\n"
                      f"[Official setup instructions]({docs}). I am guiding setup; no account or service has been created by this message.",
                      [{"label": f"Open {name}", "href": dashboard, "style": "primary"},
                       {"label": f"Use {name} for this product", "action": "service.select", "style": "secondary",
                        "payload": {"category": PROVIDERS[provider][0], "provider": provider}}])
    categories = [category] if category in {"hosting", "backend"} else ["hosting", "backend"]
    choices, actions = [], []
    if "hosting" in categories:
        choices.append("Hosting puts your product on a public web address. Start with Netlify for a small web app; use Render if it needs a server process; Free sleeps when idle. Vercel Hobby is personal/non-commercial only.")
    if "backend" in categories:
        choices.append("A database remembers user data, sign-in identifies people, and file storage keeps uploads. Supabase combines all three; Neon supplies the database alone. Choose only what your product needs.")
    for key, (kind, name, tradeoff, _dashboard, _docs) in PROVIDERS.items():
        if kind in categories:
            actions.append({"label": f"Choose {name}", "action": "service.select", "style": "secondary",
                            "payload": {"category": kind, "provider": key}})
    return question_reply("\n\n".join(choices) + "\n\nChoose a service below and I will show the exact setup steps. Accounts and credentials are not connected yet.", actions)


def select_service(venture_id: str, payload: dict) -> dict:
    provider, category = payload.get("provider"), payload.get("category")
    if (set(payload) - {"provider", "category"} or not isinstance(provider, str) or provider not in PROVIDERS
            or PROVIDERS[provider][0] != category):
        return _reply("Choose one of the service buttons. I did not save any settings or credentials.")
    with db()() as session:
        # Lock the parent before the first document exists so concurrent choices
        # preserve both categories instead of racing the first insert.
        if session.get(Venture, venture_id, with_for_update=True) is None:
            raise KeyError("venture_not_found")
        values = _load(session, venture_id)
        values[category] = provider
        _save(session, venture_id, "service_plan", values)
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief
    invalidate_venture_state_brief(venture_id)
    return guide(venture_id, provider)


def _public_url(value: object) -> str | None:
    if not isinstance(value, str) or not 8 <= len(value) <= 500 or re.search(r"[\s\\]", value):
        return None
    from app.secrets.detect import looks_like_secret
    if looks_like_secret(value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.port not in {None, 443}):
            return None
        host = url.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None


def record_product(venture_id: str, payload: dict) -> dict:
    url = _public_url(payload.get("url"))
    if set(payload) != {"url"} or not url:
        return _reply("Send only a public https address without login details or a query string: Record my hosted product https://YOUR-PUBLIC-ADDRESS. Nothing was saved.")
    with db()() as session:
        if session.get(Venture, venture_id, with_for_update=True) is None:
            raise KeyError("venture_not_found")
        values = {"url": url, "services": _load(session, venture_id), "state": "founder_reported", "verified": False}
        _save(session, venture_id, "hosted_product", values, path=url)
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief
    invalidate_venture_state_brief(venture_id)
    return _reply("Saved your product address and chosen services as founder-provided. I have not verified the app, sign-in or database. "
                  "Open it signed out, finish the main user flow, reload and check the saved result. Tell me which step passed or failed.",
                  [{"label": "Open product", "href": url, "style": "primary"},
                   {"label": "Verify it now", "action": "host.verify", "style": "secondary", "payload": {"url": url}}])


def coding_guidance(venture_id: str, *, artifact_profile: str | None = None) -> str:
    with db()() as session:
        choices = _load(session, venture_id)
    if not choices:
        return ""
    selected = ", ".join(f"{kind}: {PROVIDERS[provider][1]}" for kind, provider in sorted(choices.items()))
    if artifact_profile == "static_landing_v1":
        text = (f"Founder-chosen services: {selected}. These are choices, not proof of accounts, credentials or connections. "
                "For service setup accompanying this static landing implementation, prepare DEPLOY.md only: document the chosen hosting dashboard, "
                "workspace-root index.html, bundled assets and output directory. Record backend choices as future product work only. "
                "Do not add .env files, database migrations, backend services, authentication or payment processing. "
                "Do not read, print or invent credentials. Do not create accounts, connect services, sign up or deploy. "
                "Do not claim setup or live-site success; source capture still requires separate independent QA and founder review. ")
        if choices.get("hosting") == "render":
            text += "Document a static-site setup; Free web services can idle and lose local files, so do not assume an always-on backend. "
        if choices.get("hosting") == "vercel":
            text += "Hobby is only for personal non-commercial use; do not deploy a startup there under Hobby. "
        return text[:1800]
    text = (f"Founder-chosen product services: {selected}. These are choices, not proof of accounts or credentials. "
            "Prepare DEPLOY.md with exact dashboard clicks, framework/root folder, build/start command and output folder; "
            "include .env.example containing variable NAMES and empty values only; ignore real env files. "
            "Keep elevated keys and database URLs server-only. Do not read, print or invent credentials. "
            "Provide reviewed database migrations, per-user authorization and tests for save/reload and cross-user isolation. "
            "Do not claim setup, connection, deployment or live-product success without evidence. Do not sign up or deploy in this coding task. ")
    if choices.get("backend") == "supabase":
        text += ("Use current Supabase publishable keys for browser access and row-level security on exposed tables. "
                 "For Next.js list NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY; "
                 "SUPABASE_SECRET_KEY is optional server-only, never NEXT_PUBLIC_. ")
    elif choices.get("backend") == "neon":
        text += "Use DATABASE_URL only in server code; choose auth separately when required. "
    if choices.get("hosting") == "render":
        text += "Listen on 0.0.0.0 and platform PORT. No persistent local files on Free; explain idle sleep and preview limits. "
    if choices.get("hosting") == "vercel":
        text += "Hobby is only for personal non-commercial use; do not deploy a startup there under Hobby. "
    return text[:1800]


def snapshot(session, venture_id: str) -> dict:
    row = session.get(VentureDocument, f"hosted-product:{venture_id}")
    own = bool(row and row.venture_id == venture_id)
    try:
        saved = json.loads(row.body_md or "{}") if own else {}
    except (TypeError, ValueError):
        saved = {}
    verified = saved.get("verified") is True and saved.get("state") == "verified"
    return {"choices": _load(session, venture_id),
            "product_url": _public_url(row.path) if own else None,
            "state": "verified" if verified else "founder_reported" if own else "not_recorded",
            "verified": verified, "verified_at": saved.get("verified_at") if verified else None,
            "title": saved.get("title") if verified else None, "host": saved.get("host") if own else None}


def status(venture_id: str) -> dict:
    with db()() as session:
        saved = snapshot(session, venture_id)
    url = saved["product_url"]
    if not url:
        return _reply("No product address is recorded. After deploying and testing it, send: Record my hosted product https://YOUR-PUBLIC-ADDRESS.")
    selected = ", ".join(PROVIDERS[value][1] for value in saved["choices"].values()) or "none recorded"
    if saved["verified"]:
        return _reply(f"Your hosted product is {url}, verified by Supymem on {str(saved['verified_at'])[:10]} (HTTP 200, title "
                      f"\"{saved['title']}\"). Chosen services: {selected}. Sign-in and saved data are not tested; the landing-page waitlist is tracked separately.",
                      [{"label": "Open product", "href": url, "style": "primary"}])
    return _reply(f"Your reported product address is {url}. Chosen services: {selected}. "
                  "The app, sign-in and database are unverified; the landing-page waitlist is tracked separately.",
                  [{"label": "Open product", "href": url, "style": "primary"}])


def matches(text: str) -> bool:
    if re.match(r"(?i)\s*(?:what(?: is|\'s)|show|how is).{0,80}\bstatus\b", text):
        return False
    if re.match(r"(?i)\s*(?:show|open) my (?:hosted )?product(?: address)?\s*[.?!]?\s*$", text):
        return True
    return bool(re.search(r"(?i)^\s*(?:record my hosted product\b|(?:use|choose|set up|setup|connect|configure)\s+(?:a\s+)?(?:supabase|neon|netlify|render|vercel)\b|(?:how|where|help|guide|show|what).{0,100}\b(?:host(?:ing)?|deploy|deployment|backend|database|supabase|neon|netlify|render|vercel)\b)", text))


def from_text(venture_id: str, text: str) -> dict:
    if re.match(r"(?i)\s*(?:show|open) my (?:hosted )?product(?: address)?\s*[.?!]?\s*$", text):
        return status(venture_id)
    record = re.fullmatch(r"(?i)\s*record my hosted product\s+(\S+)\s*", text)
    if record:
        return record_product(venture_id, {"url": record[1]})
    provider = next((key for key in PROVIDERS if re.search(rf"\b{key}\b", text, re.I)), "")
    if provider and re.match(r"(?i)\s*(?:use|choose)\b", text):
        return select_service(venture_id, {"provider": provider, "category": PROVIDERS[provider][0]})
    category = "backend" if re.search(r"(?i)\b(?:database|backend|sign.in|storage)\b", text) else "hosting"
    return guide(venture_id, provider, category)

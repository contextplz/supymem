import type { ChatMessage } from "./api-types";
import { isLocalConsole } from "./localMode.ts";
import { ventureGrowthHrefs } from "./operationsScope.ts";
import { isLocalWorkPage } from "./chatVisibility.ts";

const ROUTE_LABELS: Record<string, string> = {
  idea: "Review idea",
  now: "Open next actions",
  research: "Review research",
  plan: "Review plan",
  builds: "Review builds",
  tasks: "Review tasks",
};

/** Turn a saved assistant route link into a navigation CTA, including older replies without actions. */
export function chatRouteAction(message: ChatMessage, ventureId: string): NonNullable<ChatMessage["actions"]> {
  if (message.role !== "assistant" || message.actions?.length) return [];
  const body = message.body_md || message.text || "";
  for (const match of body.matchAll(/(?:https?:\/\/|app\.supymem\.com\/|\/v\/)[^\s<>()\]]+/gi)) {
    // Do not read a /v/ substring from another host, protocol, or path.
    const before = body[match.index - 1] || "";
    if (match[0].startsWith("/v/") && /[\w/:]/.test(before)) continue;
    if (match[0].toLowerCase().startsWith("app.supymem.com/") && /[\w./]/.test(before)) continue;
    const candidate = match[0].replace(/[.,;!?]+$/, "");
    let url: URL;
    try {
      url = new URL(candidate.startsWith("/v/") ? candidate
        : candidate.toLowerCase().startsWith("app.supymem.com/") ? `https://${candidate}` : candidate,
      "https://app.supymem.com");
    } catch {
      continue;
    }
    if (url.protocol !== "https:" || url.hostname !== "app.supymem.com"
      || url.username || url.password || url.port) continue;
    const native = isLocalWorkPage(url.pathname) && isLocalConsole();
    if (!native && (url.search || url.hash)) continue;
    const prefix = `/v/${encodeURIComponent(ventureId)}/`;
    if (!url.pathname.startsWith(prefix)) continue;
    const route = url.pathname.slice(prefix.length);
    const label = native && /^research\/?$/.test(route) ? "Open report" : ROUTE_LABELS[route];
    if (label) return [{ label, href: `${url.pathname}${url.search}${url.hash}`, style: "primary" }];
  }
  return [];
}

type ChatAction = NonNullable<ChatMessage["actions"]>[number];
const CONNECTIONS = /^(?:\/v\/([^/?#]+))?\/connections\/?$/;

function localConnectionsHref(path: string, ventureId?: string): string | undefined {
  if (!/^\/(?:v\/[^/?#]+\/)?connections(?:[?/#]|$)/.test(path) || !isLocalConsole()) return;
  const url = new URL(path, "https://app.supymem.com");
  const match = CONNECTIONS.exec(url.pathname);
  if (!match) return;
  try {
    const workspace = url.searchParams.get("workspace");
    const venture = match[1] ? decodeURIComponent(match[1]) : url.searchParams.get("venture") || (workspace ? undefined : ventureId);
    if (!venture) return `/connections${url.search}`;
    return ventureGrowthHrefs(venture).connections + (workspace ? `&workspace=${encodeURIComponent(workspace)}` : "");
  } catch { return undefined; }
}

/** Chat-first: links to hidden pages become the in-chat action that shows the same thing (mirrors navigation_map._HIDDEN). */
const IN_CHAT: Array<[RegExp, ChatAction, boolean?]> = [
  [/^\/v\/[^/?#]+\/?(?:\?[^#]*)?(?:#next)?$/, { label: "Choose your next step", action: "next_moves" }],
  [/^\/(?:content|media)(?:[?/#]|$)/, { label: "Show your drafts", action: "content.drafts" }, true],
  [/^\/(?:marketing|community|ads|ops\/(?:ai-gtm|ads))(?:[?/#]|$)/, { label: "Show customers and drafts", action: "gtm.status" }, true],
  [/^\/(?:lab|pagebuilder-lab)(?:[?/#]|$)/, { label: "What should I do next?", action: "whats_next" }, true],
  [/^\/ops\/finance(?:[?/#]|$)/, { label: "Show your finances", action: "money" }, true],
  [/^\/ops\/social(?:[?/#]|$)/, { label: "Show your social accounts", action: "social_status" }, true],
  [/^\/ops\/agents(?:[?/#]|$)/, { label: "Show your team", action: "agents" }, true],
  [/^\/ops(?:\/(?:tasks|memory))?(?:[?/#]|$)/, { label: "Show status here", action: "status" }, true],
  [/^\/v\/[^/]+\/idea\/?(?:[?#]|$)/, { label: "Show my idea review here", action: "idea.review" }],
  [/^\/gtm(?:[?/#]|$)|^\/v\/[^/]+\/(?:gtm|outreach)(?:[?/#]|$)|^\/ops\/outreach(?:[?/]|$)/, { label: "Show customers and drafts", action: "gtm.status" }],
  [/^\/v\/[^/]+\/research(?:[?/#]|$)/, { label: "Show research here", action: "research" }],
  [/^\/v\/[^/]+\/connections(?:[?/#]|$)|^\/connections(?:[?/#]|$)/, { label: "Show connections here", action: "connections.status" }],
  [/^\/v\/[^/]+\/site\?(?:[^#]*&)?build=1(?:[&#]|$)/, { label: "Build my landing page", action: "pagebuilder.build", payload: { prepare: true } }],
  [/^\/v\/[^/]+\/site(?:[?/#]|$)/, { label: "Show my website here", action: "site.preview" }],
  [/^\/v\/[^/]+\/experiments(?:[?/#]|$)/, { label: "Show experiments here", action: "experiment.list" }],
  [/^\/v\/[^/]+\/context(?:[?/#]|$)/, { label: "Review my answers here", action: "startup.review" }],
  [/^\/v\/[^/]+\/suggestions(?:[?/#]|$)/, { label: "What should I do next?", action: "whats_next" }],
  [/^\/v\/[^/]+\/(?:org|people)(?:[?/#]|$)/, { label: "Show my team here", action: "agents" }],
  [/^\/v\/[^/]+\/(?:now|work|tasks|plan|execution|charter|builds|analytics|settings|market|feedback|evolution|import|simulate)(?:[?/#]|$)|^\/(?:tasks|execution)(?:[?/#]|$)/,
    { label: "Show status here", action: "status" }],
];

export function isChatNavigationAction(action: ChatAction): boolean {
  if ((action.action || action.name) === "idea.review" && action.style === "primary" || action.reply_choice === true) return false;
  return IN_CHAT.some(([, target]) => !target.payload && target.action === (action.action || action.name));
}

export function inChatActions(actions: ChatAction[], chatFirst = true, ventureId?: string): ChatAction[] {
  const seen = new Set(actions.filter(a => !a.href).map(a => a.action));
  const out: ChatAction[] = [];
  for (const a of actions) {
    const secure = "kind" in a && Boolean((a as { kind?: unknown }).kind);
    const link = typeof a.href === "string" ? a.href.replace(/^https:\/\/app\.supymem\.com(?=\/)/, "") : "";
    const connections = !secure || ("kind" in a && a.kind === "recovery_link") ? localConnectionsHref(link, ventureId) : undefined;
    if (connections) { out.push({ ...a, href: connections }); continue; }
    const path = secure ? "" : link;
    const target = /^\/v\/([^/?#]+)/.exec(path)?.[1];
    if (arguments.length > 2 && target && target !== encodeURIComponent(ventureId || "")) { out.push(a); continue; }
    if (isLocalWorkPage(path) && isLocalConsole() && !/^\/v\/[^/]+\/experiments\/?\?(?:[^#]*&)?experiment_id=/.test(path)) { out.push(a); continue; }
    const officeTab = /^\/v\/[^/?#]+\/?(?:[?#]|$)/.test(path)
      && ["archives", "toolkit", "departments"].includes(new URL(path, "https://app.supymem.com").searchParams.get("office") || "");
    const swap = path && !officeTab ? IN_CHAT.find(([pattern, target, homeOnly]) => (chatFirst || !homeOnly && target.action !== "idea.review") && pattern.test(path))?.[1] : undefined;
    if (!swap) out.push(a);
    else if (!seen.has(swap.action)) { seen.add(swap.action); out.push({ ...swap, style: a.style || "ghost" }); }
  }
  return out;
}

export const HOME_NAVIGATION_EVENT = "supymem:home-navigation";

export function savedBuildActivity(ventureId: string, run: string | null): string | undefined {
  if (run && /^dwo_[A-Za-z0-9_-]{1,124}$/.test(run)) {
    return `/v/${encodeURIComponent(ventureId)}/device-work-orders/${run}`;
  }
}

export function homeRouteAction(href: string, ventureId?: string): { action?: string; payload?: unknown; sidebar?: "settings" | "toolkit" | "view"; section?: "account" | "mac" | "manager" | "venture"; href?: string } | null {
  if (!href.startsWith("/") || href.startsWith("//")) return null;
  const localConnections = localConnectionsHref(href, ventureId);
  if (localConnections) return { sidebar: "view", href: localConnections };
  const url = new URL(href, "https://app.supymem.com");
  if (url.origin !== "https://app.supymem.com" || url.username || url.password) return null;
  const target = /^\/v\/([^/]+)(?:\/|$)/.exec(url.pathname)?.[1];
  const global = /^\/(?:gtm|connections|ops\/outreach|ops\/settings|tasks|execution|account)(?:\/|$)/.test(url.pathname);
  const action = inChatActions([{ label: "Open", href }])[0];
  const settings = /^(?:\/v\/[^/]+\/settings|\/account|\/ops\/settings)\/?$/.test(url.pathname);
  const connections = /^(?:\/v\/[^/]+\/connections|\/connections)\/?$/.test(url.pathname);
  const localView = isLocalWorkPage(url.pathname) && isLocalConsole() && action?.action !== "experiment.list";
  const view = localView || /^\/v\/[^/]+\/(?:device-work-orders\/[^/]+|idea\/current|linkedin|leads)\/?$/.test(url.pathname);
  if (!settings && !connections && !view && !action?.action) return null;
  const requested = target ? decodeURIComponent(target) : url.searchParams.get("venture") || ventureId;
  if (requested && requested !== ventureId) return { href: `/v/${encodeURIComponent(requested)}?chat=1&chat_route=${encodeURIComponent(href)}` };
  if (!ventureId && !global) return null;
  if (settings) return { sidebar: "settings", section: url.pathname === "/account" ? "account" : url.hash === "#god-mode" ? "manager" : /computer-use/.test(url.hash) || url.pathname === "/ops/settings" ? "mac" : "venture" };
  if (connections && ventureId) return { sidebar: "toolkit" };
  if (view) return { sidebar: "view", href: localView && requested && /\/builds\/?$/.test(url.pathname)
    ? savedBuildActivity(requested, url.searchParams.get("run")) || href : href };
  if (action?.action === "experiment.list" && url.searchParams.get("experiment_id")) {
    return { action: action.action, payload: { experiment_id: url.searchParams.get("experiment_id") } };
  }
  return action?.action && ventureId ? { action: action.action, payload: action.payload } : null;
}

export function routeInChat(href: string): boolean {
  if (typeof document === "undefined" || typeof window === "undefined") return false;
  const shell = document.querySelector?.<HTMLElement>(".home-shell");
  if (!shell) return false;
  let url: URL;
  try { url = new URL(href, window.location.href); } catch { return false; }
  if (url.username || url.password || url.origin !== window.location.origin && url.origin !== "https://app.supymem.com") return false;
  let target;
  try { target = homeRouteAction(`${url.pathname}${url.search}${url.hash}`, shell.dataset.ventureId); } catch { return false; }
  if (!target) return false;
  if (url.origin === window.location.origin && url.pathname === "/connections" && target.sidebar === "view" && target.href === `${url.pathname}${url.search}${url.hash}`) return false;
  window.dispatchEvent(new CustomEvent(HOME_NAVIGATION_EVENT, { detail: target }));
  return true;
}

const chatRouters = new WeakMap<object, object>();

export function chatNavigation<Router extends { push: (href: string, options?: { scroll?: boolean }) => void; replace: (href: string, options?: { scroll?: boolean }) => void }>(router: Router): Router {
  const saved = chatRouters.get(router);
  if (saved) return saved as Router;
  const guarded = { ...router,
    push: (href: string, options?: { scroll?: boolean }) => { if (!routeInChat(href)) router.push(href, options); },
    replace: (href: string, options?: { scroll?: boolean }) => { if (!routeInChat(href)) router.replace(href, options); },
  };
  chatRouters.set(router, guarded);
  return guarded;
}

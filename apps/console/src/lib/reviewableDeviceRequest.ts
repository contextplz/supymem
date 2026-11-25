/**
 * Client gate for Mac-task phrases that should POST to from-chat.
 *
 * Keep this in sync with `classify_device_chat_intent` in
 * `services/company_os/app/build/device_orders.py` via
 * `tests/fixtures/device_chat_intent_parity.json`. The server still chooses
 * the saved plan, device, action, budget and review. Ordinary chat must not
 * hit device work orders by accident. Local coding requests go to server chat.
 * The client gate must never be broader
 * than the server classifier (word-boundary checks vs server substring checks
 * for coding verbs are an intentional narrowing).
 */
import { leadingDesktopAppScope } from "./desktopAppMention.ts";

const MAC_CONTROL_LEADIN = /^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?use\s+(?:my|this|the)\s+mac\s+to\s+/i;
const READONLY_BROWSER_LEADIN = /^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?use\s+(?:my|this|the)\s+mac\s+for\s+(?:an?\s+)?(?:read-only|read only|readonly)\s+(?:background\s+)?browser\s+task\b/i;

function nonAuthorizingMacMention(text: string): boolean {
  const readonlyMention = /use\s+(?:my|this|the)\s+mac\s+for\s+(?:an?\s+)?(?:read-only|read only|readonly)\s+(?:background\s+)?browser\s+task\b/i;
  if (readonlyMention.test(text) && !READONLY_BROWSER_LEADIN.test(text)) return true;
  return /^\s*(?:please\s+)?(?:do\s+not|don['’]t|never)\s+use\s+(?:my|this|the)\s+mac\s+to\b/i.test(text)
    || (!MAC_CONTROL_LEADIN.test(text)
      && /["'“]\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?use\s+(?:my|this|the)\s+mac\s+to\b/i.test(text));
}

function normalizedText(text: string): string {
  return text.toLowerCase().replace(/\s+/g, " ").trim();
}

function namedProvider(normalized: string): boolean {
  return /\b(?:cursor|codex|claude|opencode)\b/.test(normalized);
}

function requestsWorkspaceMutation(normalized: string): boolean {
  if (/\b(?:implement|develop|build)\b/.test(normalized)) return true;
  for (const match of normalized.matchAll(/\bedits?\b|\bediting\b/g)) {
    const preceding = normalized.slice(Math.max(0, match.index - 24), match.index);
    if (!/\b(?:do not|don't|without|no)\b/.test(preceding)) return true;
  }
  return false;
}

function readonlyInspection(normalized: string): boolean {
  if (!namedProvider(normalized) || requestsWorkspaceMutation(normalized)) return false;
  return /\b(?:read-only|read only|readonly|inspect|inspection)\b/.test(normalized)
    || /do not edit|don't edit|without editing|without edits|no edits/.test(normalized);
}

/** Saved venture screens are first-party chat requests when the founder only asks to view/report. */
function firstPartyReadOnlyView(text: string): boolean {
  let command = text.trim();
  const explicitMacView = MAC_CONTROL_LEADIN.test(command) && /\bsupymem\b/i.test(command);
  if (explicitMacView) command = command.replace(MAC_CONTROL_LEADIN, "");
  const constraint = /(?:[.!?;]\s*)?(?:answer only|do not (?:start|create|queue|modify|change|delete|edit|run)\b[^.!?]*)[.!?]?\s*$/i;
  while (constraint.test(command)) command = command.replace(constraint, "").trim();
  const screens = explicitMacView
    ? "cockpit|dashboard|idea|now|work|tasks|team|website|research|market|feedback|plan|execution|builds|analytics|import|evolution|people|connections|settings|experiments|content|gtm|go to market|go-to-market"
    : "analytics|builds";
  const match = new RegExp("^(?:(?:can|could|would|will)\\s+you\\s+)?(?:please\\s+)?(?:open|show(?:\\s+me)?|go\\s+to|take\\s+me\\s+to|navigate\\s+to)\\s+(?:(?:the|my|this\\s+venture(?:'s)?)\\s+)?(?:[a-z0-9][a-z0-9 _-]{0,79}?\\s+)?(?:" + screens + ")(?:\\s+(?:page|screen|tab))?\\s+in\\s+supymem(?:\\s+and\\s+(?:(?:tell|show|report)\\s+(?:me\\s+)?)(.+))?\\s*[.!?]?$", "i").exec(command);
  if (!match) return false;
  const report = match[1];
  return !report || (/^(?:what|which)\b/i.test(report)
    && !/\b(?:click|press|select|clear|type|write|enter|scroll|delete|edit|change|modify|start|run|create|submit|launch)\b/i.test(report));
}

// "Prepare a Mac task: open TextEdit, ..." names the action; the task itself follows.
// Keep in sync with _MAC_TASK_LEADIN in device_orders.py.
const MAC_TASK_LEADIN = /^\s*(?:please\s+)?(?:prepare|set up|create|queue)\s+(?:a|the|this)\s+mac task\s*(?::|,|-|\bto\b|\bthat\b)\s*/i;

/** "Yes, that folder is right. Prepare the Mac task for review." confirms the previous request. */
export function bareMacTaskConfirmation(text: string): boolean {
  return !reviewableDeviceRequest(text) && !text.trim().endsWith("?")
    && /\b(?:prepare|set up|queue)\s+(?:the|that|this)\s+mac task(?:\s+for review)?\s*[.!]?\s*$/i.test(text);
}

/** A precise explicit request whose policy is refused by the server before preparation. */
export function readonlyBrowserTaskRequest(text: string): boolean {
  return READONLY_BROWSER_LEADIN.test(text.replace(MAC_TASK_LEADIN, ""));
}

/** Reading a public page with no Mac named is answered in chat. Keep in sync with site_read_url in chat/link_context.py. */
export function siteReadRequest(text: string): boolean {
  return /^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:open|visit|read|browse|inspect|check(?:\s+out)?|look\s+at|go\s+to|navigate\s+to)\s+(?:(?:the\s+)?(?:website|site|page)\s+(?:at\s+)?)?(?:https?:\/\/)?(?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?!(?:js|ts|py|md|txt|pdf|app|png|jpe?g|docx?|xlsx?|csv|json|html?)\b)[a-z]{2,}(?:[\/?#]\S*)?(?=[\s,.;:!?]|$)/i.test(text)
    && !/\b(?:sign\s*-?up|signup|register|log\s*-?in|sign\s+in|create\s+(?:an?\s+)?account|api\s+key|fill|submit|form|click|press|type|enter|buy|pay|checkout|download|delete|change|edit|waitlist)\b/i.test(text.replace(/\b(?:do\s+not|don['’]t|never|without)\b[^.,;!?]*/gi, ""))
    && !/\b(?:my|this|the)\s+(?:mac(?:book)?|computer|laptop|desktop|pc|machine)\b|\bmy\s+browser\b|\b(?:in|using|with)\s+(?:the\s+)?(?:chrome|safari|firefox|edge|brave|arc|browser)\b/i.test(text);
}

export function browserTaskRequest(text: string): boolean {
  if (siteReadRequest(text.replace(MAC_TASK_LEADIN, ""))) return false;
  if (/waitlist/i.test(text) && /\b(?:live|published|verify)\b/i.test(text)) return false;
  const match = new RegExp(String.raw`^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?<deviceScope>(?:on|using)\s+(?:my|this|the)\s+(?:mac(?:book)?|computer|laptop|desktop|pc|machine)\b\s*,?\s*)?(?:please\s+)?(?:use\s+(?:my|this|the)\s+(?:browser|mac)\s+to\s+(?:check|visit|look up|compare|sign up for|go to)\b|(?:use\s+)?computer use\b|(?<deviceUse>use\s+(?:my|this|the)\s+(?:mac(?:book)?|computer|laptop|desktop|pc|machine)\s+to\s+)?(?:open|visit|browse|check|inspect|read|navigate\s+to|go\s+to)\s+(?:https?:\/\/|(?<site>(?:(?:a|the|my|this)\s+)?(?:website|site|web)\b)))`, "i").exec(text.replace(MAC_TASK_LEADIN, ""));
  return Boolean(match && (!match.groups?.site || match.groups.deviceScope || match.groups.deviceUse || /https?:\/\//i.test(text)));
}

/** True when the server can prepare a review or return a specific capability refusal. */
export function reviewableDeviceRequest(text: string, local = false): boolean {
  text = text.replace(MAC_TASK_LEADIN, "");
  if (nonAuthorizingMacMention(text)) return false;
  const normalized = normalizedText(text);
  if (!normalized) return false;
  if (local) {
    const request = normalized.replace(/\b(?:do not|don['’]t|never|without)\b[^.!?]*/g, "");
    if (/\b(?:build|rebuild|redo|implement|develop|create|make|write|change|update|fix|improve|edit)\b/.test(request)
        && (/\b(?:page|site|website|product|app|application|code|backend|scraper|tool|full[ -]stack)\b/.test(request)
          || namedProvider(request))) return false;
  }

  // A founder asking about an already approved task may quote an action in
  // the question. Neither that quote nor a trailing "do not start" constraint
  // is authority to prepare a second order. Polite Mac control requests
  // remain eligible for review ("Can you open Notes on this Mac?").
  if (/^(?:what|which|where|when|why|how|did|does|is|are|was|were|has|should|do you|have you)\b/.test(normalized)) return false;

  // The general browser profile cannot bind a no-mutation policy. Route this
  // exact request to its truthful refusal, never to a free-form "prepared" reply.
  if (readonlyBrowserTaskRequest(text)) return true;

  // Read-only first-party views use venture chat and its saved-data tools.
  if (firstPartyReadOnlyView(text)) return false;

  if (browserTaskRequest(text)) return true;
  if (desktopControlRequest(text)) return true;

  if (/\b(?:supymem|this app)\b/.test(normalized)
      && /\b(?:open|show|navigate)\b/.test(normalized)
      && /\b(?:builds|settings)\b/.test(normalized)) return true;

  if (readonlyInspection(normalized)) return true;

  // Coding requires a named provider plus an edit/build verb. A bare provider
  // mention stays in ordinary chat; Settings / Mac-tasks entry still posts it.
  // Word boundaries intentionally exclude substring hits such as "codex"/"coding"
  // matching the server's `"code" in normalized` check.
  if (namedProvider(normalized)
      && /\b(?:code|build|implement|develop|edit)\b/.test(normalized)
      && !readonlyInspection(normalized)) return true;

  if (/\bterminal\b/.test(normalized)
      && /\b(?:git status|workspace status|workspace readiness)\b/.test(normalized)
      && /\b(?:check|run|verify)\b/.test(normalized)) return true;

  return /^(?:please\s+|(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:open|check|review|verify)\b/.test(normalized)
    && /\bwaitlist\b/.test(normalized)
    && /\b(?:browser|site|page|url)\b/.test(normalized)
    && /\b(?:check|open|review|verify)\b/.test(normalized)
    || (
      /\b(?:go to|open|visit|browse)\b/.test(normalized)
      && (/\b(?:sign up|signup|register|create an account|create account|get an api key|get api key)\b/.test(normalized)
        || /\b(?:prepare|fill(?: out)?|complete)\s+(?:(?:the|a)\s+)?(?:(?:signup|sign-up|registration|application)\s+)?form\b/.test(normalized))
      && /(?:https?:\/\/)?(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+/i.test(normalized)
    );
}

/** An explicit request to control this Mac, rather than a question about it. */
export function desktopControlRequest(text: string): boolean {
  text = text.replace(MAC_TASK_LEADIN, "");
  if (siteReadRequest(text)) return false;
  if (nonAuthorizingMacMention(text)) return false;
  if (readonlyBrowserTaskRequest(text)) return false;
  if (firstPartyReadOnlyView(text)) return false;
  // Named CLI coding does not need screen capture or native app control, even
  // when the founder asks it to write tests "on this Mac".
  if (namedProvider(normalizedText(text))
      && /^\s*(?:(?:can|could|would|will)\s+you\s+|i\s+(?:want|would like)\s+(?:you\s+)?to\s+|i['’]d\s+like\s+(?:you\s+)?to\s+|help\s+me\s+|let['’]?s\s+)?(?:please\s+)?(?:(?:build|implement|develop)\b|use\s+(?:codex|claude(?: code)?)\s+to\s+(?:build|implement|develop)\b)/i.test(text)) return false;
  // A leading request names this Mac explicitly. Preserve the whole goal for
  // the existing native inventory, scoped prepare and saved review flow.
  // Keep in sync with explicit_mac_control_request in chat/mac_offers.py.
  if (/^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?use\s+(?:my|this|the)\s+mac\s+to\s+(?:open|launch|go to|visit|navigate|browse|search|click|press|select|clear|type|write|enter|scroll|inspect|read|report|describe)\b/i.test(text)) return true;
  const normalized = normalizedText(text);
  // A leading installed-app scope is a direct command even without the magic
  // words "on this Mac". The inventory resolver must still find exactly one
  // installed app and an exact window before a review can be prepared.
  const leading = leadingDesktopAppScope(text);
  const likelyAppScope = Boolean(leading && (!/^(?:my|the|this|a|an)\b/i.test(leading.clause)
    || /\b(?:in|of)\s+[a-z][a-z0-9 .'-]+$/i.test(leading.clause)));
  const appScopedImperative = likelyAppScope
    && /^(?:please\s+)?(?:click|press|select|clear|type|write|enter|create|save|scroll|inspect|read|open|launch|go to|search)\b/i.test(leading!.remainder)
    && !normalized.endsWith("?");
  if (appScopedImperative) return true;
  // The first clause can be a condition on the selected app's current screen.
  // The following imperative still requires a reviewed native observation.
  const appScopedConditional = likelyAppScope
    && /^if\b[^,?.]{1,160},\s*(?:then\s+)?(?:please\s+)?(?:only\s+)?(?:click|press|select|clear|type|write|enter|scroll|inspect|read|open|launch|go to|search|report|describe)\b/i.test(leading!.remainder)
    && !normalized.endsWith("?");
  if (appScopedConditional) return true;
  // The founder may put the condition before the app action. Keep the app
  // name at the start so the inventory resolver can bind one exact app.
  const leadingAppCondition = /^if\s+(?!my\b|the\b|this\b|a\b|an\b)[a-z][a-z0-9 .'-]{1,50}?\s+(?:is|has|shows|displays)\b[^,?.]{1,160},\s*(?:then\s+)?(?:please\s+)?(?:only\s+)?(?:click|press|select|clear|type|write|enter|scroll|inspect|read|open|launch|go to|search|report|describe)\b/i.test(text.trim())
    && !normalized.endsWith("?");
  if (leadingAppCondition) return true;
  // A named installed application followed by an action is a Mac request even
  // when the founder does not say "my browser" or "on this Mac" explicitly.
  const namedAppAction = /^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:open|launch)\s+(?!my\b|the\b|this\b|a\b|an\b)[a-z][a-z0-9 .'-]{1,50}?(?=\s*(?:,|\band\b|\bthen\b|\.(?=\s+(?:use|press|click|select|clear|type|write|enter|create|save|scroll|inspect|read|search|go to)\b)))/i.test(text)
    && /\b(?:go to|search|click|press|select|clear|type|write|enter|create|save|scroll|inspect|read|tell|report|describe)\b/i.test(text);
  const politeAction = /^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:open|launch|go to|visit|navigate|browse|search|click|press|select|clear|type|write|enter|scroll|inspect)\b/.test(normalized);
  return ((!/^(?:how|can|does|what|why|is|are|will|should|could|would)\b/.test(normalized)
      && !normalized.endsWith("?")) || politeAction)
    && (/\b(?:my browser|on my mac|on this mac|use my computer|computer use)\b/.test(normalized)
      || namedAppAction)
    && /\b(?:open|launch|go to|visit|navigate|browse|search|click|press|select|clear|type|write|enter|scroll|inspect)\b/.test(normalized);
}

/** Venture chat posts to from-chat only for reviewableDeviceRequest. */
export function ventureChatDeviceRoute(text: string): "from-chat" | "chat" {
  return reviewableDeviceRequest(text) ? "from-chat" : "chat";
}

function isQuestion(normalized: string, original: string): boolean {
  if (original.trim().endsWith("?")) return true;
  return /^(?:how|can|does|what|why|is|are|will|should|could|would)\b/.test(normalized);
}

/**
 * True when ordinary chat should still run, then append a local Mac-tasks hint.
 * Requires an explicit Mac-control marker AND an imperative control verb.
 * Questions never get the hint. Never routes to from-chat by itself.
 */
export function shouldAppendMacTasksHint(text: string): boolean {
  if (reviewableDeviceRequest(text)) return false;
  const original = text.trim();
  const normalized = normalizedText(text);
  if (!normalized || isQuestion(normalized, original)) return false;
  const hasMarker = /\b(?:on this mac|on my mac|computer use|use my computer|mac task)\b/.test(normalized);
  const hasVerb = /\b(?:open|launch|quit|type|click|control|drive)\b/.test(normalized);
  return hasMarker && hasVerb;
}

export const MAC_TASKS_HINT =
  "Mac tasks require a reviewed scope. Choose a coding, browser or terminal example in chat, or check current availability in Settings → computer use. Native app tasks require explicit availability; when available, Supymem will show the selected app, window, permitted actions, and credit ceiling before anything runs. If it cannot resolve a safe scope, it will explain the blocker.";

export const SUPPORTED_MAC_TASK_EXAMPLES = [
  {
    label: "Inspect Calculator",
    desktop: true,
    text: "Open Calculator and tell me exactly what number is shown. Do not press any buttons.",
  },
  {
    label: "Git status",
    text: "Check git status in the terminal for the venture workspace.",
  },
  {
    label: "Cursor inspect",
    text: "Inspect the workspace with Cursor, read-only; do not edit files.",
  },
  {
    label: "Live site",
    text: "Open the browser and check the live site page and waitlist.",
  },
  {
    label: "Browser signup goal",
    text: "Go to resend.com and prepare the signup form without submitting or entering credentials",
  },
] as const;

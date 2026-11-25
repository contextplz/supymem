/** Parse ACP (Cursor) and Claude stream-json lines into a transcript. */
import { safeLoopText } from "./loopProgress.ts";

export type TranscriptItem = {
  kind: "you" | "agent" | "thought" | "tool" | "status" | "error";
  text: string;
  tool?: string;
  toolStatus?: string;
};

export type PermissionAsk = {
  rpcId: number | string;
  title: string;
};

export type ParsedLine = {
  items: TranscriptItem[];
  permission?: PermissionAsk;
  sessionId?: string;
  stop?: boolean;
};

type Json = Record<string, unknown>;

function asObj(v: unknown): Json | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : null;
}

function textOf(v: unknown): string {
  if (typeof v === "string") return v;
  const o = asObj(v);
  if (!o) return "";
  if (typeof o.text === "string") return o.text;
  if (typeof o.content === "string") return o.content;
  return "";
}

const SESSION_UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i;

/** ACP session id from a JSON-RPC result or a Hermes log line. */
export function extractAcpSessionId(raw: string): string | undefined {
  const text = raw.replace(/^\[stderr\]\s*/i, "");
  const jsonish =
    text.match(/"sessionId"\s*:\s*"([^"]+)"/i) || text.match(/"session_id"\s*:\s*"([^"]+)"/i);
  if (jsonish?.[1]) return jsonish[1];
  const created = text.match(/Created ACP session\s+([0-9a-f-]{36})/i);
  if (created?.[1] && SESSION_UUID.test(created[1])) return created[1];
  const createdNew = text.match(/New session\s+([0-9a-f-]{36})/i);
  if (createdNew?.[1] && SESSION_UUID.test(createdNew[1])) return createdNew[1];
  return undefined;
}

function sessionFromUnknown(v: unknown): string | undefined {
  if (typeof v === "string" && SESSION_UUID.test(v)) return v;
  const o = asObj(v);
  if (!o) return undefined;
  if (typeof o.sessionId === "string") return o.sessionId;
  if (typeof o.session_id === "string") return o.session_id;
  return sessionFromUnknown(o.session);
}

/** Split a stdio chunk into lines and merge transcript + session id. */
export function parseControlChunk(provider: string, raw: string): ParsedLine {
  const lines = raw.replace(/\r/g, "").split("\n").filter((l) => l.trim());
  if (lines.length <= 1) return parseControlLine(provider, raw);
  const merged: ParsedLine = { items: [] };
  for (const line of lines) {
    const one = parseControlLine(provider, line);
    merged.items.push(...one.items);
    if (one.sessionId) merged.sessionId = one.sessionId;
    if (one.permission) merged.permission = one.permission;
    if (one.stop) merged.stop = true;
  }
  return merged;
}

export function parseControlLine(provider: string, raw: string): ParsedLine {
  const parsed = parseUnsafeControlLine(provider, raw);
  return { ...parsed, items: parsed.items.map(item => ({ ...item, text: safeLoopText(item.text, 2000),
    tool: item.tool ? safeLoopText(item.tool) : undefined })),
    permission: parsed.permission ? { ...parsed.permission, title: safeLoopText(parsed.permission.title) } : undefined };
}

function parseUnsafeControlLine(provider: string, raw: string): ParsedLine {
  const line = raw.replace(/\r/g, "").trim();
  if (!line) return { items: [] };
  const sidFromText = extractAcpSessionId(line);
  if (line.startsWith("[stderr]")) {
    return {
      items: [{ kind: "status", text: "Your tool sent a diagnostic update. Check your build activity for the next result." }],
      sessionId: sidFromText,
    };
  }
  let msg: Json;
  try {
    msg = JSON.parse(line) as Json;
  } catch {
    return { items: [{ kind: "status", text: "Your tool sent an update. Keep watching your build for the next result." }], sessionId: sidFromText };
  }
  if (!asObj(msg)) return {items: []};
  if (provider === "claude") return parseClaude(msg);
  if (provider === "codex") return parseCodex(msg);
  if (provider === "opencode") return parseOpenCode(msg);
  const parsed = parseAcp(msg);
  if (!parsed.sessionId && sidFromText) parsed.sessionId = sidFromText;
  return parsed;
}

function parseOpenCode(msg: Json): ParsedLine {
  const sessionId = typeof msg.sessionID === "string" ? msg.sessionID : undefined;
  const part = asObj(msg.part), state = asObj(part?.state), input = asObj(state?.input);
  if (part?.type === "tool") {
    const text = String(input?.command || input?.filePath || part.tool || "Working in your project folder");
    return {sessionId, items: [{kind: state?.status === "error" ? "error" : "tool", text,
      tool: String(part.tool || "tool"), toolStatus: String(state?.status || "running")}]};
  }
  if (msg.type === "error") return {sessionId, items: [{kind: "error", text: String(asObj(msg.error)?.message || msg.message || "Your coding tool stopped. Check its login, then retry your build.")}]};
  if (msg.type === "result") return {sessionId, items: [{kind: "status", text: "Your tool finished this request. Open your result folder to check the saved work."}], stop: true};
  const text = textOf(part?.text) || textOf(msg.text);
  return {sessionId, items: text ? [{kind: "agent", text}] : []};
}

function parseAcp(msg: Json): ParsedLine {
  const method = typeof msg.method === "string" ? msg.method : "";
  if (method === "session/update") {
    const params = asObj(msg.params);
    const update = asObj(params?.update) || params;
    const kind = String(update?.sessionUpdate || update?.session_update || "");
    const content = update?.content;
    const text = textOf(content) || textOf(update);
    if (kind === "agent_thought_chunk" && text) {
      return { items: [{ kind: "thought", text }] };
    }
    if (kind === "tool_call" || kind === "tool_call_update") {
      const title = String(update?.title || update?.kind || "tool");
      const status = String(update?.status || kind);
      return { items: [{ kind: "tool", text: title, tool: title, toolStatus: status }] };
    }
    if (text) return { items: [{ kind: "agent", text }] };
    return { items: [] };
  }
  if (method === "session/request_permission" && msg.id != null) {
    const params = asObj(msg.params);
    const toolCall = asObj(params?.toolCall);
    const title = String(params?.title || toolCall?.title || "Allow this tool?");
    return {
      items: [{ kind: "status", text: `Permission: ${title}` }],
      permission: { rpcId: msg.id as number | string, title },
    };
  }
  if (msg.error) {
    const err = asObj(msg.error);
    return { items: [{ kind: "error", text: String(err?.message || "ACP error") }] };
  }
  const result = asObj(msg.result);
  const sid = sessionFromUnknown(result) || sessionFromUnknown(msg);
  if (sid) {
    return { items: [], sessionId: sid };
  }
  if (result && typeof result.stopReason === "string") {
    return { items: [{ kind: "status", text: `Turn ended (${result.stopReason})` }], stop: true };
  }
  return { items: [] };
}

function parseClaude(msg: Json): ParsedLine {
  const type = String(msg.type || "");
  if (type === "system" && typeof msg.session_id === "string") {
    return { items: [{ kind: "status", text: "Claude session ready" }], sessionId: msg.session_id };
  }
  if (type === "assistant") {
    const message = asObj(msg.message);
    const content = Array.isArray(message?.content) ? message.content : [];
    const items: TranscriptItem[] = [];
    for (const block of content) {
      const b = asObj(block);
      if (!b) continue;
      if (b.type === "text" && typeof b.text === "string") {
        items.push({ kind: "agent", text: b.text });
      }
      if (b.type === "tool_use") {
        const command = b.name === "Bash" ? asObj(b.input)?.command : undefined;
        const title = typeof command === "string" ? command : String(b.name || "tool");
        items.push({
          kind: "tool",
          text: title,
          tool: title,
          toolStatus: "running",
        });
      }
    }
    return { items };
  }
  if (type === "content_block_delta") {
    const delta = asObj(msg.delta);
    if (delta?.type === "text_delta" && typeof delta.text === "string") {
      return { items: [{ kind: "agent", text: delta.text }] };
    }
  }
  if (type === "result") {
    const sid = typeof msg.session_id === "string" ? msg.session_id : undefined;
    return {
      items: [{ kind: "status", text: "Turn finished" }],
      sessionId: sid,
      stop: true,
    };
  }
  if (type === "error" || msg.error) {
    return { items: [{ kind: "error", text: String(msg.error || msg.message || "error") }] };
  }
  return { items: [] };
}

function parseCodex(msg: Json): ParsedLine {
  const type = String(msg.type || msg.kind || "");
  if (type === "thread.started" && typeof msg.thread_id === "string") {
    return { items: [{ kind: "status", text: "Codex session ready" }], sessionId: msg.thread_id };
  }
  if (type === "turn.completed" || type === "turn.failed") {
    return { items: [{ kind: "status", text: type === "turn.failed" ? "Turn failed" : "Turn finished" }], stop: true };
  }
  const item = asObj(msg.item);
  const itemType = String(item?.type || "");
  if (type === "item.started" || type === "item.completed" || type === "item.updated") {
    if (itemType === "command_execution" || itemType === "command" || itemType === "mcp_tool_call") {
      const title = String(item?.command || item?.name || "shell");
      return {
        items: [
          {
            kind: "tool",
            text: title,
            tool: title,
            toolStatus: type === "item.completed" ? "completed" : "running",
          },
        ],
      };
    }
    const text = textOf(item?.text) || textOf(item?.content) || textOf(msg);
    if (text) return { items: [{ kind: "agent", text }] };
  }
  if (typeof msg.session_id === "string") {
    return { items: [], sessionId: msg.session_id };
  }
  if (msg.error) {
    return { items: [{ kind: "error", text: String(asObj(msg.error)?.message || msg.error) }] };
  }
  const text = textOf(msg);
  if (text) return { items: [{ kind: "agent", text }] };
  return { items: [] };
}

export function acpLine(id: number, method: string, params: unknown): string {
  return `${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`;
}

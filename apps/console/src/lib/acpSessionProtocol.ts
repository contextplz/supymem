/** Bounded ACP framing and correlation. This never grants tool authority. */
export type RpcId = string | number;
type ObjectValue = Record<string, unknown>;
export type PermissionOption = { optionId: string; kind: "allow_once" | "reject_once"; name: string };
export type AcpEvent =
  | { kind: "reply"; id: RpcId; result?: ObjectValue; error?: string }
  | { kind: "activity"; sessionId: string; text: string; toolId?: string; status?: string }
  | { kind: "permission"; sessionId: string; id: RpcId; title: string; options: PermissionOption[] }
  | { kind: "protocol_error"; message: string };
const object = (value: unknown): ObjectValue | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as ObjectValue : null;
const rpcId = (value: unknown): value is RpcId => typeof value === "string" && value.length > 0 && value.length <= 128 || typeof value === "number" && Number.isSafeInteger(value);
const bounded = (value: unknown, max = 1000): string => typeof value === "string" ? value.slice(0, max) : "";

/** One instance per native process. Logs and another session cannot become control messages. */
export class AcpSessionProtocol {
  private buffer = "";
  private sessionId: string | null = null;
  private requests = new Map<RpcId, string>();
  private permissions = new Map<RpcId, PermissionOption[]>();
  private seenPermissions = new Set<RpcId>();
  private broken = false;
  private readonly processId: string;
  private readonly maxFrame: number;
  constructor(processId: string, maxFrame = 262144) {
    this.processId = processId;
    this.maxFrame = maxFrame;
  }

  request(id: RpcId, method: "initialize" | "session/new" | "session/prompt"): void {
    if (this.broken || !rpcId(id) || this.requests.has(id) || this.requests.size >= 8) throw new Error("ACP request cannot be registered.");
    this.requests.set(id, method);
  }

  feed(processId: unknown, chunk: unknown): AcpEvent[] {
    if (this.broken || processId !== this.processId || typeof chunk !== "string") return [];
    if (chunk.length > this.maxFrame * 4) return [this.fail()];
    this.buffer += chunk;
    const events: AcpEvent[] = [];
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, "");
      this.buffer = this.buffer.slice(newline + 1);
      if (line.length > this.maxFrame) return [...events, this.fail()];
      if (!line.trim() || line.startsWith("[stderr]")) continue;
      let raw: unknown;
      try { raw = JSON.parse(line); } catch { return [...events, this.fail()]; }
      const event = this.accept(object(raw));
      if (event) events.push(event);
      if (this.broken) return events;
    }
    if (this.buffer.length > this.maxFrame) events.push(this.fail());
    return events;
  }

  private accept(message: ObjectValue | null): AcpEvent | null {
    if (!message || message.jsonrpc !== "2.0") return this.fail();
    if (typeof message.method !== "string") {
      const id = message.id;
      if (!rpcId(id) || !this.requests.has(id)) return null;
      const method = this.requests.get(id);
      this.requests.delete(id);
      if (message.error) return { kind: "reply", id, error: "The local agent rejected this request." };
      const result = object(message.result);
      if (!result) return this.fail();
      if (method === "initialize" && result.protocolVersion !== 1) return this.fail();
      if (method === "session/new") {
        const sid = result.sessionId;
        if (typeof sid !== "string" || !sid || sid.length > 256 || this.sessionId) return this.fail();
        this.sessionId = sid;
      }
      return { kind: "reply", id, result };
    }
    const params = object(message.params);
    if (!this.sessionId || params?.sessionId !== this.sessionId) return null;
    if (message.method === "session/request_permission") {
      const id = message.id;
      if (!rpcId(id)) return this.fail();
      if (this.seenPermissions.has(id)) return null;
      if (this.seenPermissions.size >= 1024) return this.fail();
      if (this.permissions.size >= 16) return this.fail();
      const options: PermissionOption[] = [];
      const candidates = Array.isArray(params.options) ? params.options : [];
      for (const raw of candidates.slice(0, 16)) {
        const option = object(raw);
        if (!option || (option.kind !== "allow_once" && option.kind !== "reject_once") || typeof option.optionId !== "string" || !option.optionId || option.optionId.length > 128) continue;
        if (options.some(saved => saved.optionId === option.optionId)) return this.fail();
        options.push({ optionId: option.optionId, kind: option.kind, name: bounded(option.name, 120) });
      }
      this.permissions.set(id, options);
      this.seenPermissions.add(id);
      return { kind: "permission", sessionId: this.sessionId, id, options,
        title: bounded(object(params.toolCall)?.title, 500) || "Review this local tool action" };
    }
    if (message.method === "session/update") {
      const update = object(params.update);
      if (!update) return null;
      if (update.sessionUpdate === "agent_thought_chunk") return null;
      if (update.sessionUpdate === "agent_message_chunk") {
        return { kind: "activity", sessionId: this.sessionId, text: bounded(object(update.content)?.text, 4000) };
      }
      if (update.sessionUpdate === "tool_call" || update.sessionUpdate === "tool_call_update") {
        return { kind: "activity", sessionId: this.sessionId, text: bounded(update.title, 500),
          toolId: bounded(update.toolCallId, 128), status: bounded(update.status, 40) };
      }
    }
    return null;
  }

  /** Return exactly one offered, one-shot option. No fallback to an always-allow grant. */
  answer(id: RpcId, optionId: string | null): string {
    if (this.broken || !this.permissions.has(id)) throw new Error("This permission request is no longer active.");
    const options = this.permissions.get(id)!;
    if (optionId !== null && !options.some(option => option.optionId === optionId)) throw new Error("This option was not offered for this request.");
    this.permissions.delete(id);
    const outcome = optionId === null ? { outcome: "cancelled" } : { outcome: "selected", optionId };
    return JSON.stringify({ jsonrpc: "2.0", id, result: { outcome } }) + "\n";
  }

  close(): void {
    this.broken = true;
    this.buffer = "";
    this.requests.clear();
    this.permissions.clear();
    this.seenPermissions.clear();
  }

  private fail(): AcpEvent {
    this.close();
    return { kind: "protocol_error", message: "The local agent sent an invalid or oversized control message. Review the session before restarting." };
  }
}

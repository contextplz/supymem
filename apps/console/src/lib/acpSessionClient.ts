import { AcpSessionProtocol, type AcpEvent, type RpcId } from "./acpSessionProtocol.ts";

type Reply = Extract<AcpEvent, { kind: "reply" }>;
export type SessionEvent = Exclude<AcpEvent, { kind: "reply" }> | { kind: "disconnected"; message: string };
export type SessionBridge = {
  invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
  listen: (name: string, callback: (payload: unknown) => void) => Promise<() => void>;
};

/** Retains correlated listeners until explicit disposal. A session is not an execution receipt. */
export class AcpSessionClient {
  private bridge: SessionBridge;
  private emit: (event: SessionEvent) => void;
  private protocol: AcpSessionProtocol | null = null;
  private processId: string | null = null;
  private sessionId: string | null = null;
  private serial = 1;
  private closed = false;
  private opening = false;
  private prompting = false;
  private subscriptions: (() => void)[] = [];
  private pending = new Map<RpcId, { resolve: (reply: Reply) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  private timeoutMs: number;
  private termination: Promise<unknown> | null = null;

  constructor(bridge: SessionBridge, emit: (event: SessionEvent) => void, timeoutMs = 45000) {
    this.bridge = bridge;
    this.emit = emit;
    this.timeoutMs = timeoutMs;
  }

  async open(ventureId: string, provider: string, workspace: string | (() => Promise<string>)): Promise<string> {
    if (this.closed || this.opening || this.processId) throw new Error("This local session cannot be started again.");
    this.opening = true;
    try {
      await this.subscribe("stdio-output", payload => {
        const rec = payload as { id?: unknown; data?: unknown } | null;
        for (const event of this.protocol?.feed(rec?.id, rec?.data) || []) {
          if (event.kind === "reply") {
            const waiter = this.pending.get(event.id);
            if (!waiter) continue;
            clearTimeout(waiter.timer);
            this.pending.delete(event.id);
            if (event.error) waiter.reject(new Error(event.error));
            else waiter.resolve(event);
          } else {
            this.emit(event);
            if (event.kind === "protocol_error") this.disconnect(event.message);
          }
        }
      });
      await this.subscribe("stdio-exit", payload => {
        const rec = payload as { id?: unknown } | null;
        if (this.processId && rec?.id === this.processId) {
          this.disconnect("The local agent disconnected. Its exit alone does not verify completion.");
        }
      });
      if (this.closed) throw new Error("The local session was cancelled.");
      const id = await this.bridge.invoke("spawn_coding_control", {
        provider, ventureId, autoApprove: false, mode: "acp",
      });
      if (typeof id !== "string" || !id) throw new Error("The Mac app did not return a local process identifier.");
      this.processId = id;
      if (this.closed) {
        await this.terminate();
        throw new Error("The local session was cancelled during startup.");
      }
      this.protocol = new AcpSessionProtocol(id);
      await this.request("initialize", {
        protocolVersion: 1,
        clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
        clientInfo: { name: "supymem-desktop", version: "0.1.0" },
      });
      const cwd = typeof workspace === "function" ? await workspace() : workspace;
      if (!cwd?.trim()) throw new Error("The Mac app did not provide this venture's workspace.");
      const reply = await this.request("session/new", { cwd, mcpServers: [] });
      const sid = reply.result?.sessionId;
      if (typeof sid !== "string" || !sid) throw new Error("The local agent did not establish a session.");
      this.sessionId = sid;
      return sid;
    } catch (error) {
      this.dispose();
      if (this.processId) {
        try { await this.terminate(); }
        catch { throw new Error("Local startup failed and the Mac app could not confirm stopping the process. Review it before restarting."); }
      }
      throw error;
    }
  }

  async prompt(text: string): Promise<string> {
    if (!this.sessionId || this.closed || this.prompting) throw new Error("The local session is unavailable or already working.");
    this.prompting = true;
    try {
      const reply = await this.request("session/prompt", {
        sessionId: this.sessionId, prompt: [{ type: "text", text }],
      });
      const reason = reply.result?.stopReason;
      if (typeof reason !== "string" || !["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"].includes(reason)) {
        this.disconnect("The local agent returned an unrecognized completion result.");
        throw new Error("The local agent did not provide a valid turn result.");
      }
      return reason;
    } finally { this.prompting = false; }
  }

  async answer(id: RpcId, optionId: string | null): Promise<void> {
    if (this.closed || !this.processId || !this.protocol) throw new Error("This local session is no longer active.");
    const data = this.protocol.answer(id, optionId);
    try { await this.bridge.invoke("stdio_write", { id: this.processId, data }); }
    catch (error) {
      this.disconnect("The permission reply could not be delivered. Do not retry this action automatically.");
      throw error;
    }
  }

  async stop(): Promise<void> {
    this.dispose();
    await this.terminate();
  }

  dispose(): void {
    this.closed = true;
    this.protocol?.close();
    for (const unsub of this.subscriptions.splice(0)) unsub();
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(new Error("The local session closed before its response arrived."));
    }
    this.pending.clear();
  }

  private disconnect(message: string): void {
    if (this.closed) return;
    this.dispose();
    this.emit({ kind: "disconnected", message });
    if (this.processId) {
      void this.terminate().catch(() => {
        this.emit({ kind: "disconnected", message: "The Mac app could not confirm stopping the disconnected local process. Review it before restarting." });
      });
    }
  }

  private terminate(): Promise<unknown> {
    if (!this.processId) return Promise.resolve();
    if (!this.termination) this.termination = this.bridge.invoke("kill_coding_control", { id: this.processId });
    return this.termination;
  }

  private async subscribe(name: string, callback: (payload: unknown) => void): Promise<void> {
    if (this.closed) throw new Error("The local session was cancelled.");
    const unsub = await this.bridge.listen(name, callback);
    if (this.closed) unsub();
    else this.subscriptions.push(unsub);
  }

  private request(method: "initialize" | "session/new" | "session/prompt", params: unknown): Promise<Reply> {
    if (this.closed || !this.protocol || !this.processId) return Promise.reject(new Error("The local session is closed."));
    const id = this.serial++;
    this.protocol.request(id, method);
    return new Promise<Reply>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("The local agent response timed out. Inspect the session before restarting."));
        this.disconnect("The local session timed out; no automatic retry was made.");
      }, method === "session/prompt" ? Math.max(this.timeoutMs, 30 * 60 * 1000) : this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      void this.bridge.invoke("stdio_write", {
        id: this.processId, data: JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n",
      }).catch(error => {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
        this.disconnect("The Mac app could not deliver a local-agent request.");
      });
    });
  }
}

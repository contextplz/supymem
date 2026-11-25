import test from "node:test";
import assert from "node:assert/strict";
import { AcpSessionClient, type SessionBridge, type SessionEvent } from "./acpSessionClient.ts";

function fixture(options: { failWrite?: boolean; silent?: boolean } = {}) {
  const callbacks = new Map<string, (payload: unknown) => void>();
  const calls: string[] = [];
  const events: SessionEvent[] = [];
  const send = (message: unknown, id = "process-a") => callbacks.get("stdio-output")?.({ id, data: JSON.stringify(message) + "\n" });
  const bridge: SessionBridge = {
    listen: async (name, callback) => { calls.push(`listen:${name}`); callbacks.set(name, callback); return () => { callbacks.delete(name); }; },
    invoke: async (command, args) => {
      calls.push(command);
      if (command === "spawn_coding_control") {
        assert.equal(args?.autoApprove, false);
        assert.equal(callbacks.size, 2);
        return "process-a";
      }
      if (command === "stdio_write") {
        if (options.failWrite) throw new Error("broken pipe");
        const frame = JSON.parse(String(args?.data));
        if (frame.method && !options.silent) {
          send({ jsonrpc: "2.0", id: frame.id, result: frame.method === "session/new" ? { sessionId: "session-a" } : frame.method === "session/prompt" ? { stopReason: "end_turn" } : { protocolVersion: 1 } });
        }
      }
      return null;
    },
  };
  const client = new AcpSessionClient(bridge, event => events.push(event), 30);
  return { client, bridge, callbacks, calls, events, send };
}

test("listeners precede handshake, synchronous replies are retained, turn completion is correlated", async () => {
  const f = fixture();
  assert.equal(await f.client.open("venture-a", "hermes", "/approved/path"), "session-a");
  assert.equal(await f.client.prompt("Perform the approved step"), "end_turn");
  assert.equal(f.callbacks.size, 2);
  assert.deepEqual(f.calls.slice(0, 3), ["listen:stdio-output", "listen:stdio-exit", "spawn_coding_control"]);
  await f.client.stop();
  assert.equal(f.callbacks.size, 0);
  await assert.rejects(f.client.prompt("repeat"), /unavailable/);
});

test("permission listeners survive startup; only exact offered choices are delivered once", async () => {
  const f = fixture();
  await f.client.open("venture-a", "hermes", "/approved/path");
  f.send({ jsonrpc: "2.0", id: 81, method: "session/request_permission", params: { sessionId: "session-a", toolCall: { title: "Read the approved project" }, options: [{ optionId: "permit-81", kind: "allow_once", name: "Allow once" }] } });
  assert.equal(f.events[0].kind, "permission");
  await assert.rejects(f.client.answer(81, "allow-once"), /not offered/);
  await f.client.answer(81, "permit-81");
  await assert.rejects(f.client.answer(81, "permit-81"), /no longer active/);
  await f.client.stop();
});

test("foreign process exit cannot end this session and EOF does not claim completion", async () => {
  const f = fixture();
  await f.client.open("venture-a", "hermes", "/approved/path");
  f.callbacks.get("stdio-exit")?.({ id: "foreign-process" });
  assert.equal(f.events.length, 0);
  f.callbacks.get("stdio-exit")?.({ id: "process-a" });
  assert.equal(f.events.at(-1)?.kind, "disconnected");
  assert.equal(f.callbacks.size, 0);
  await assert.rejects(f.client.prompt("repeat"), /unavailable/);
});

test("failed writes close listeners and stop the process without retrying", async () => {
  const f = fixture({ failWrite: true });
  await assert.rejects(f.client.open("venture-a", "hermes", "/approved/path"), /broken pipe/);
  assert.equal(f.callbacks.size, 0);
  assert.equal(f.calls.filter(call => call === "spawn_coding_control").length, 1);
  assert.equal(f.calls.filter(call => call === "kill_coding_control").length, 1);
});

test("missing handshake response times out, cleans up and never sends a prompt", async () => {
  const f = fixture({ silent: true });
  await assert.rejects(f.client.open("venture-a", "hermes", "/approved/path"), /timed out/);
  assert.equal(f.callbacks.size, 0);
  assert.equal(f.calls.filter(call => call === "stdio_write").length, 1);
});

test("unmount during listener registration removes late subscriptions and never spawns", async () => {
  const f = fixture();
  let release: (() => void) | undefined;
  let removed = 0;
  f.bridge.listen = () => new Promise(resolve => { release = () => resolve(() => { removed++; }); });
  const opening = f.client.open("venture-a", "hermes", "/approved/path");
  f.client.dispose();
  release?.();
  // A closed client should not register the second listener.
  await assert.rejects(opening, /cancelled/);
  assert.equal(removed, 1);
  assert.equal(f.calls.includes("spawn_coding_control"), false);
});

test("native rejection precedes workspace creation", async () => {
  const f = fixture();
  const invoke = f.bridge.invoke;
  f.bridge.invoke = async (command, args) => {
    if (command === "spawn_coding_control") throw new Error("approved device work order required");
    return invoke(command, args);
  };
  let workspaceCalls = 0;
  await assert.rejects(f.client.open("venture-a", "hermes", async () => { workspaceCalls++; return "/approved/path"; }), /work order required/);
  assert.equal(workspaceCalls, 0);
  assert.equal(f.callbacks.size, 0);
});

import assert from "node:assert/strict";
import test from "node:test";
import {
  DOCK_HEIGHT_DEFAULT,
  DOCK_STORAGE_KEY,
  DOCK_WIDTH_DEFAULT,
  DOCK_WIDTH_MAX,
  DOCK_WIDTH_MIN,
  CHAT_DRAFT_MAX_CHARS,
  canStartSend,
  composerPlaceholder,
  chatDraftKey,
  clearChatDraftIfMatches,
  clampDockSize,
  composerHeightPx,
  draftAfterFailedSend,
  draftStorage,
  nudgeDockSize,
  readStoredDockSize,
  readChatDraft,
  shouldSendOnKey,
  writeStoredDockSize,
  writeChatDraft,
} from "./chatComposer.ts";

test("draft storage uses localStorage, survives a new window, and handles storage denial and size limits", () => {
  const values = new Map<string, string>();
  const storage = {getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {values.set(key, value);}, removeItem: (key: string) => {values.delete(key);}};
  const original = Object.getOwnPropertyDescriptor(globalThis, "window");
  try {
    Object.defineProperty(globalThis, "window", {configurable: true, value: {localStorage: storage, sessionStorage: null}});
    assert.equal(draftStorage(), storage);
    writeChatDraft(draftStorage(), "draft", "Unsent founder text");
    Object.defineProperty(globalThis, "window", {configurable: true, value: {localStorage: storage}});
    assert.equal(readChatDraft(draftStorage(), "draft"), "Unsent founder text");
    const boundary = "x".repeat(CHAT_DRAFT_MAX_CHARS);
    writeChatDraft(storage, "draft", boundary);
    assert.equal(readChatDraft(storage, "draft"), boundary);
    writeChatDraft(storage, "draft", boundary + "x");
    assert.equal(readChatDraft(storage, "draft"), boundary);
    storage.setItem("draft", boundary + "x");
    assert.equal(readChatDraft(storage, "draft"), "");
    const denied = {getItem: () => {throw new Error("denied");}, setItem: () => {throw new Error("quota");}, removeItem: () => {throw new Error("denied");}};
    assert.equal(readChatDraft(denied, "draft"), "");
    assert.doesNotThrow(() => writeChatDraft(denied, "draft", "text"));
    assert.doesNotThrow(() => clearChatDraftIfMatches(denied, "draft", "text"));
    Object.defineProperty(globalThis, "window", {configurable: true, value: {get localStorage() {throw new Error("denied");}}});
    assert.equal(draftStorage(), null);
  } finally {
    if (original) Object.defineProperty(globalThis, "window", original);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("an interrupted chat keeps its draft within the same account and venture", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
  const key = chatDraftKey("usr_one", "ven_alpha");
  assert.ok(key);
  writeChatDraft(storage, key, "Need to finish my idea");
  assert.equal(readChatDraft(storage, key), "Need to finish my idea");
  assert.equal(readChatDraft(storage, chatDraftKey("usr_two", "ven_alpha")), "");
  assert.equal(readChatDraft(storage, chatDraftKey("usr_one", "ven_beta")), "");
  assert.equal(chatDraftKey(undefined, "ven_alpha"), null);

  // The old request finishing after reconnect cannot erase new founder typing.
  writeChatDraft(storage, key, "New message after reconnect");
  clearChatDraftIfMatches(storage, key, "Need to finish my idea");
  assert.equal(readChatDraft(storage, key), "New message after reconnect");
  clearChatDraftIfMatches(storage, key, "New message after reconnect");
  assert.equal(readChatDraft(storage, key), "");
});

test("connection-page chat draft survives an auth gate and stays account scoped", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
  const ventureId = "ven_connect";
  // The page can mount before its account check resolves. Nothing is stored
  // until the verified subject supplies the same key used by the chat dock.
  assert.equal(chatDraftKey(undefined, ventureId), null);
  const firstKey = chatDraftKey("usr_one", ventureId);
  writeChatDraft(storage, firstKey, "Question about this connection");
  assert.equal(readChatDraft(storage, firstKey), "Question about this connection");
  // Reauthorization unmounts the page; a fresh instance restores the text.
  assert.equal(readChatDraft(storage, chatDraftKey("usr_one", ventureId)), "Question about this connection");
  assert.equal(readChatDraft(storage, chatDraftKey("usr_two", ventureId)), "");
});

test("private New Chat drafts are separate while shared legacy history keeps its existing draft key", () => {
  const shared = chatDraftKey("usr_one", "ven_alpha");
  assert.equal(chatDraftKey("usr_one", "ven_alpha", null), shared);
  const first = chatDraftKey("usr_one", "ven_alpha", "conversation_saved_1");
  assert.notEqual(first, shared);
  assert.notEqual(first, chatDraftKey("usr_one", "ven_alpha", "conversation_saved_2"));
  assert.notEqual(first, chatDraftKey("usr_two", "ven_alpha", "conversation_saved_1"));
  assert.equal(chatDraftKey("usr_one", "ven_alpha", "../other"), null);
});

test("canStartSend clears path: rejects empty and blocks while pending", () => {
  assert.equal(canStartSend("hello", false), true);
  assert.equal(canStartSend("  ", false), false);
  assert.equal(canStartSend("hello", true), false);
});

test("draftAfterFailedSend restores when box still empty after optimistic clear", () => {
  assert.equal(
    draftAfterFailedSend({ failedText: "ship it", currentDraft: "" }),
    "ship it",
  );
});

test("draftAfterFailedSend does not clobber newer founder typing", () => {
  assert.equal(
    draftAfterFailedSend({ failedText: "ship it", currentDraft: "new thought" }),
    "new thought",
  );
});

test("Enter sends; Shift+Enter and IME composition do not", () => {
  assert.equal(shouldSendOnKey({ key: "Enter", shiftKey: false }), true);
  assert.equal(shouldSendOnKey({ key: "Enter", shiftKey: true }), false);
  assert.equal(shouldSendOnKey({ key: "Enter", shiftKey: false, isComposing: true }), false);
  assert.equal(shouldSendOnKey({ key: "Enter", shiftKey: false, keyCode: 229 }), false);
  assert.equal(
    shouldSendOnKey({
      key: "Enter",
      shiftKey: false,
      nativeEvent: { isComposing: true },
    }),
    false,
  );
  assert.equal(
    shouldSendOnKey({
      key: "Enter",
      shiftKey: false,
      nativeEvent: { keyCode: 229 },
    }),
    false,
  );
  assert.equal(shouldSendOnKey({ key: "a", shiftKey: false }), false);
});

test("composerHeightPx grows then caps at 8 lines or 40% dock", () => {
  const one = composerHeightPx({ scrollHeight: 30 });
  assert.equal(one.height, 30);
  assert.equal(one.capped, false);

  const grown = composerHeightPx({ scrollHeight: 22 * 4 + 8 });
  assert.equal(grown.height, 22 * 4 + 8);
  assert.equal(grown.capped, false);

  const maxByLines = composerHeightPx({ scrollHeight: 900 });
  assert.equal(maxByLines.height, 22 * 8 + 8);
  assert.equal(maxByLines.capped, true);

  const dockCap = composerHeightPx({ scrollHeight: 900, dockHeight: 200 });
  // 40% of 200 = 80, which is below 8-line max
  assert.equal(dockCap.height, 80);
  assert.equal(dockCap.capped, true);
});

test("clampDockSize enforces width 320-720 and height ±25%", () => {
  const short = clampDockSize({ width: 100, height: 10 });
  assert.equal(short.width, DOCK_WIDTH_MIN);
  assert.equal(short.height, Math.round(DOCK_HEIGHT_DEFAULT * 0.75));

  const tall = clampDockSize({ width: 999, height: 9999 });
  assert.equal(tall.width, DOCK_WIDTH_MAX);
  assert.equal(tall.height, Math.round(DOCK_HEIGHT_DEFAULT * 1.25));

  const mid = clampDockSize({ width: DOCK_WIDTH_DEFAULT, height: DOCK_HEIGHT_DEFAULT });
  assert.deepEqual(mid, { width: DOCK_WIDTH_DEFAULT, height: DOCK_HEIGHT_DEFAULT });
});

test("dock size persists and rehydrates from localStorage", () => {
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
  };
  writeStoredDockSize({ width: 500, height: 700 }, storage);
  assert.ok(store.has(DOCK_STORAGE_KEY));
  const loaded = readStoredDockSize(storage);
  assert.deepEqual(loaded, clampDockSize({ width: 500, height: 700 }));
});

test("PR dock default is 400px without overwriting a founder's saved width", () => {
  assert.equal(DOCK_WIDTH_DEFAULT, 400);
  assert.equal(clampDockSize({}).width, 400);
  assert.equal(readStoredDockSize({ getItem: () => JSON.stringify({ width: 430, height: 620 }) })?.width, 430);
});

test("nudgeDockSize moves by edge and stays in bounds", () => {
  const base = { width: DOCK_WIDTH_DEFAULT, height: DOCK_HEIGHT_DEFAULT };
  const wider = nudgeDockSize(base, "width", 40);
  assert.equal(wider.width, DOCK_WIDTH_DEFAULT + 40);
  const tooWide = nudgeDockSize(base, "width", 999);
  assert.equal(tooWide.width, DOCK_WIDTH_MAX);
  const taller = nudgeDockSize(base, "height", 20);
  assert.equal(taller.height, DOCK_HEIGHT_DEFAULT + 20);
});

/** Mirrors useChatComposer optimistic clear / restore / pending without React. */
test("composer send state machine: clear, restore, no-restore, no double-send", async () => {
  type State = { draft: string; pending: boolean; error: string; sent: string[] };
  let state: State = { draft: "first note", pending: false, error: "", sent: [] };

  async function submit(
    onSend: (body: string) => Promise<boolean>,
  ): Promise<boolean> {
    const body = state.draft.trim();
    if (!canStartSend(body, state.pending)) return false;
    state = { ...state, pending: true, error: "", draft: "" };
    try {
      const ok = await onSend(body);
      if (!ok) {
        state = {
          ...state,
          draft: draftAfterFailedSend({ failedText: body, currentDraft: state.draft }),
          error: "",
        };
        return false;
      }
      return true;
    } finally {
      state = { ...state, pending: false };
    }
  }

  // Optimistic clear on success
  let resolveSend!: (ok: boolean) => void;
  const pendingSend = new Promise<boolean>((r) => {
    resolveSend = r;
  });
  const first = submit(async (body) => {
    state.sent.push(body);
    return pendingSend;
  });
  assert.equal(state.draft, "");
  assert.equal(state.pending, true);
  // Double-send while pending is ignored
  assert.equal(await submit(async () => true), false);
  resolveSend(true);
  assert.equal(await first, true);
  assert.deepEqual(state.sent, ["first note"]);
  assert.equal(state.pending, false);

  // Restore on failure when box still empty
  state = { ...state, draft: "retry me", error: "" };
  assert.equal(await submit(async () => false), false);
  assert.equal(state.draft, "retry me");
  assert.equal(state.error, "");

  // No-restore when founder typed something new during the failed send
  state = { ...state, draft: "original", error: "" };
  let midResolve!: (ok: boolean) => void;
  const mid = new Promise<boolean>((r) => {
    midResolve = r;
  });
  const second = submit(async () => mid);
  assert.equal(state.draft, "");
  state = { ...state, draft: "typed while waiting" };
  midResolve(false);
  assert.equal(await second, false);
  assert.equal(state.draft, "typed while waiting");
});

test("composer placeholder names the manager and falls back to Charlie", () => {
  assert.equal(composerPlaceholder("Maya"), "Ask Maya anything…");
  assert.equal(composerPlaceholder(), "Ask Charlie anything…");
  assert.equal(composerPlaceholder("  "), "Ask Charlie anything…");
});

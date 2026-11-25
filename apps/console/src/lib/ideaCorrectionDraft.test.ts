import assert from "node:assert/strict";
import test from "node:test";
import { clearIdeaCorrectionDraft, ideaCorrectionDraftKey, readIdeaCorrectionDraft, writeIdeaCorrectionDraft, type IdeaCorrectionScope } from "./ideaCorrectionDraft.ts";

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); },
    removeItem: (key: string) => { data.delete(key); },
  };
}

const scope: IdeaCorrectionScope = {
  workspaceId: "ws_one", ventureId: "ven_one", revisionId: "rev_one", target: "question", targetId: "question/1",
};

test("an unsent answer correction survives remount and remains scoped to its workspace, venture, revision, and question", () => {
  const storage = memoryStorage();
  const draft = { answer: "A personal bookmark helper", selectedOptions: ["Only me"] };
  writeIdeaCorrectionDraft(storage, scope, draft);
  assert.deepEqual(readIdeaCorrectionDraft(storage, scope), draft);
  for (const changed of [
    { workspaceId: "ws_other" }, { ventureId: "ven_other" }, { revisionId: "rev_other" },
    { targetId: "question/2" }, { target: "field" as const },
  ]) assert.equal(readIdeaCorrectionDraft(storage, { ...scope, ...changed }), null);
  clearIdeaCorrectionDraft(storage, scope);
  assert.equal(readIdeaCorrectionDraft(storage, scope), null);
});

test("field drafts and question drafts cannot overwrite each other; invalid storage stays optional", () => {
  const storage = memoryStorage();
  const fieldScope = { ...scope, target: "field" as const, targetId: "customer" };
  writeIdeaCorrectionDraft(storage, scope, { answer: "question draft", selectedOptions: [] });
  writeIdeaCorrectionDraft(storage, fieldScope, { answer: "field draft", selectedOptions: [] });
  assert.equal(readIdeaCorrectionDraft(storage, fieldScope)?.answer, "field draft");
  assert.equal(readIdeaCorrectionDraft(storage, scope)?.answer, "question draft");
  storage.data.set(ideaCorrectionDraftKey(scope), "{broken");
  assert.equal(readIdeaCorrectionDraft(storage, scope), null);
  const unavailable = { getItem: () => { throw Error("denied"); }, setItem: () => { throw Error("full"); }, removeItem: () => { throw Error("denied"); } };
  assert.equal(readIdeaCorrectionDraft(unavailable, scope), null);
  assert.doesNotThrow(() => writeIdeaCorrectionDraft(unavailable, scope, { answer: "x", selectedOptions: [] }));
  assert.doesNotThrow(() => clearIdeaCorrectionDraft(unavailable, scope));
});

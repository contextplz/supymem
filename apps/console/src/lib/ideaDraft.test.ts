import assert from "node:assert/strict";
import test from "node:test";
import { clearIdeaDraft, ideaDraftKey, readIdeaDraft, readPersistentIdeaDraft, writeIdeaDraft } from "./ideaDraft.ts";

test("an unsent Idea answer survives remount in its own venture and question", () => {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => { data.set(key, value); },
    removeItem: (key: string) => { data.delete(key); },
  };
  writeIdeaDraft(storage, "ven_one", "question/1", { answer: "Use Supabase for my own bot", selectedOptions: ["Only me"] });
  assert.deepEqual(readIdeaDraft(storage, "ven_one", "question/1"), { answer: "Use Supabase for my own bot", selectedOptions: ["Only me"] });
  assert.deepEqual(readIdeaDraft(storage, "ven_two", "question/1"), { answer: "", selectedOptions: [] });
  assert.deepEqual(readIdeaDraft(storage, "ven_one", "question/2"), { answer: "", selectedOptions: [] });
  clearIdeaDraft(storage, "ven_one", "question/1");
  assert.equal(data.has(ideaDraftKey("ven_one", "question/1")), false);
});

test("corrupt storage does not break Idea", () => {
  const storage = { getItem: () => "{broken", setItem: () => {}, removeItem: () => {} };
  assert.deepEqual(readIdeaDraft(storage, "ven_one", "q"), { answer: "", selectedOptions: [] });
});

test("a typed answer survives app restart and an old session draft migrates", () => {
  const storage = () => {
    const data = new Map<string, string>();
    return {
      getItem: (key: string) => data.get(key) || null,
      setItem: (key: string, value: string) => { data.set(key, value); },
      removeItem: (key: string) => { data.delete(key); },
    };
  };
  const persistent = storage();
  const firstSession = storage();
  writeIdeaDraft(firstSession, "ven_one", "q", { answer: "still typing", selectedOptions: ["Myself"] });
  assert.deepEqual(readPersistentIdeaDraft(persistent, firstSession, "ven_one", "q"),
    { answer: "still typing", selectedOptions: ["Myself"] });
  const restartedSession = storage();
  assert.deepEqual(readPersistentIdeaDraft(persistent, restartedSession, "ven_one", "q"),
    { answer: "still typing", selectedOptions: ["Myself"] });
  clearIdeaDraft(persistent, "ven_one", "q");
  clearIdeaDraft(firstSession, "ven_one", "q");
  assert.deepEqual(readPersistentIdeaDraft(persistent, restartedSession, "ven_one", "q"),
    { answer: "", selectedOptions: [] });
});

import assert from "node:assert/strict";
import test from "node:test";
import { forgetInvite, inviteErrorMessage, normalizeInviteCode, recallInvite, rememberInvite } from "./inviteCode.ts";

const mem = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) }; };

test("pasted codes are cleaned to eight upper-case characters", () => {
  assert.equal(normalizeInviteCode("  ab3d-ef7h \n"), "AB3DEF7H");
  assert.equal(normalizeInviteCode("abcdefghijkl"), "ABCDEFGH");
  assert.equal(normalizeInviteCode(""), "");
});

test("code survives into a second store when the first (this tab) is empty", () => {
  const tab = mem(), shared = mem();
  rememberInvite("abcd2345", [tab, shared], 1000);
  assert.equal(recallInvite([mem(), shared], 2000), "ABCD2345");
  forgetInvite([tab, shared]);
  assert.equal(recallInvite([tab, shared], 2000), "");
});

test("stale or legacy plain-string codes are handled", () => {
  const s = mem();
  rememberInvite("ABCD2345", [s], 0);
  assert.equal(recallInvite([s], 25 * 60 * 60 * 1000), "");
  s.setItem("supymem.invite", "wxyz2345");
  assert.equal(recallInvite([s]), "WXYZ2345");
});

test("each admission refusal has its own plain copy", () => {
  const seen = new Set<string>();
  for (const code of ["invite_code_required", "invalid_invite_code", "invite_code_exhausted", "self_invitation", "already_onboarded", "beta_full"]) {
    for (const shape of [{ error: { code } }, { detail: { code } }]) {
      const text = inviteErrorMessage(shape);
      assert.ok(text && !text.includes("_"), code);
      seen.add(text);
    }
  }
  assert.equal(seen.size, 6);
  assert.match(inviteErrorMessage({ detail: { code: "beta_full" } })!, /waitlist/);
  assert.match(inviteErrorMessage({ detail: { code: "invite_code_required" } })!, /8-character/);
  assert.equal(inviteErrorMessage({ error: { code: "invalid_grant" } }), null);
  assert.equal(inviteErrorMessage(null), null);
});

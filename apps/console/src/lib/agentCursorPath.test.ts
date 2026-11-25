import assert from "node:assert/strict";
import test from "node:test";
// @ts-ignore plain ESM shared with the Tauri overlay page
import { glide, durationMs, MIN_MS, MAX_MS } from "../../../../src-tauri/splashscreen/agent-cursor/path.mjs";

type P = { x: number; y: number };
const A: P = { x: 100, y: 100 };
const B: P = { x: 900, y: 500 };
const LEN = Math.hypot(B.x - A.x, B.y - A.y);
// Distance of p from the straight line A->B.
const offLine = (p: P) => Math.abs((B.x - A.x) * (A.y - p.y) - (A.x - p.x) * (B.y - A.y)) / LEN;
const along = (p: P) => ((p.x - A.x) * (B.x - A.x) + (p.y - A.y) * (B.y - A.y)) / LEN;

test("starts at from, ends exactly at to, never NaN", () => {
  const g = glide(A, B, 7);
  assert.deepEqual(g.sample(0), A);
  assert.deepEqual(g.sample(g.duration), B);
  assert.deepEqual(g.sample(g.duration + 500), B);
  for (let ms = 0; ms <= g.duration; ms += 7) {
    const p = g.sample(ms);
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
  }
});

test("deterministic per seed, different across seeds", () => {
  const a = glide(A, B, 42), b = glide(A, B, 42), c = glide(A, B, 43);
  for (const ms of [50, 200, 400]) assert.deepEqual(a.sample(ms), b.sample(ms));
  assert.notDeepEqual(a.sample(300), c.sample(300));
});

test("path curves away from the straight line", () => {
  let max = 0;
  const g = glide(A, B, 3);
  for (let ms = 0; ms < g.duration; ms += 5) max = Math.max(max, offLine(g.sample(ms)));
  assert.ok(max > 8 && max < 200, `max deviation ${max}`);
});

test("ease-in-out: slow at the ends, fast in the middle", () => {
  const g = glide(A, B, 5);
  const step = (ms: number) => {
    const p = g.sample(ms), q = g.sample(ms + 10);
    return Math.hypot(q.x - p.x, q.y - p.y);
  };
  assert.ok(step(0) < step(g.duration / 2) / 3);
});

test("overshoots slightly then settles on target", () => {
  const g = glide(A, B, 11);
  let peak = 0;
  for (let ms = 0; ms <= g.duration; ms += 4) peak = Math.max(peak, along(g.sample(ms)));
  assert.ok(peak > LEN && peak < LEN + 12, `overshoot ${peak - LEN}`);
});

test("duration is bounded and scales with distance", () => {
  assert.equal(durationMs(0.5), 0);
  assert.equal(glide(A, A, 1).duration, 0);
  assert.ok(durationMs(2) >= MIN_MS && durationMs(1e6) <= MAX_MS);
  assert.ok(durationMs(50) < durationMs(500) && durationMs(500) < durationMs(1500));
});

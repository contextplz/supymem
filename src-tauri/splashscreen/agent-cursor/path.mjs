// Human-like glide for the Supymem agent cursor. Pure and seedable: the page
// supplies Math.random-backed seeds, tests supply fixed ones.

export function rng(seed) {
  let a = seed >>> 0; // mulberry32
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const MIN_MS = 180;
export const MAX_MS = 1400;
const OVERSHOOT_SHARE = 0.86; // fraction of the time spent reaching the overshoot point

// Fitts-like: time grows with log2(distance / target width + 1).
export function durationMs(dist, width = 24) {
  if (dist < 1) return 0;
  return Math.min(MAX_MS, Math.max(MIN_MS, 120 + 130 * Math.log2(dist / width + 1)));
}

const smoother = (t) => t * t * t * (t * (6 * t - 15) + 10); // ease-in-out, zero speed at both ends
const easeOut = (t) => 1 - (1 - t) ** 3;
const bez = (p0, p1, p2, p3, t) => {
  const u = 1 - t;
  return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3;
};

/** Returns {duration, sample(ms) -> {x, y}}. sample(duration) is exactly `to`. */
export function glide(from, to, seed = 1, opts = {}) {
  const r = rng(seed);
  const dx = to.x - from.x, dy = to.y - from.y;
  const dist = Math.hypot(dx, dy);
  const duration = opts.duration ?? durationMs(dist);
  if (duration === 0 || dist < 1) return { duration: 0, sample: () => ({ x: to.x, y: to.y }) };
  const ux = dx / dist, uy = dy / dist, nx = -uy, ny = ux;
  // Slight random bow: control points pushed off the straight line.
  const bow = (r() < 0.5 ? -1 : 1) * dist * (0.04 + r() * 0.1);
  const c1 = { t: 0.2 + r() * 0.15, off: bow * (0.6 + r() * 0.8) };
  const c2 = { t: 0.65 + r() * 0.15, off: bow * (0.3 + r() * 0.8) };
  const over = Math.min(7, dist * 0.04) * (0.5 + r() * 0.5); // small overshoot, then settle
  const peak = { x: to.x + ux * over, y: to.y + uy * over };
  const p = (s, off) => ({ x: from.x + dx * s + nx * off, y: from.y + dy * s + ny * off });
  const k1 = p(c1.t, c1.off), k2 = p(c2.t, c2.off);
  const phase = [r() * 6.28, r() * 6.28];
  const jitter = Math.min(0.6, dist * 0.01); // px, micro hand tremor
  const settle = over > 0.5;
  return {
    duration,
    sample(ms) {
      const t = Math.min(1, Math.max(0, ms / duration));
      if (t >= 1) return { x: to.x, y: to.y };
      if (settle && t > OVERSHOOT_SHARE) {
        const s = easeOut((t - OVERSHOOT_SHARE) / (1 - OVERSHOOT_SHARE));
        return { x: peak.x + (to.x - peak.x) * s, y: peak.y + (to.y - peak.y) * s };
      }
      const s = smoother(settle ? t / OVERSHOOT_SHARE : t);
      const wob = Math.sin(Math.PI * s) * jitter; // zero at both ends
      const end = settle ? peak : to;
      return {
        x: bez(from.x, k1.x, k2.x, end.x, s) + Math.sin(ms / 37 + phase[0]) * wob,
        y: bez(from.y, k1.y, k2.y, end.y, s) + Math.sin(ms / 41 + phase[1]) * wob,
      };
    },
  };
}

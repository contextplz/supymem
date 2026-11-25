"use client";

import { useCallback, useEffect, useState } from "react";

export type DetailLevel = "brief" | "detailed";
export const DETAIL_EVENT = "supymem:detail";
const KEY = "supymem.detail";

export function readDetailLevel(): DetailLevel {
  try {
    return globalThis.localStorage?.getItem(KEY) === "detailed" ? "detailed" : "brief";
  } catch {
    return "brief";
  }
}

function writeDetailLevel(level: DetailLevel) {
  try { globalThis.localStorage?.setItem(KEY, level); } catch { /* storage blocked: still broadcast */ }
  globalThis.dispatchEvent?.(new CustomEvent(DETAIL_EVENT, { detail: level }));
}

export function useDetailLevel(): [DetailLevel, (l: DetailLevel) => void] {
  // Start at "brief" so SSR and first client render match; sync after mount.
  const [level, setLevel] = useState<DetailLevel>("brief");
  useEffect(() => {
    setLevel(readDetailLevel());
    const on = () => setLevel(readDetailLevel());
    window.addEventListener(DETAIL_EVENT, on);
    window.addEventListener("storage", on);
    return () => { window.removeEventListener(DETAIL_EVENT, on); window.removeEventListener("storage", on); };
  }, []);
  const set = useCallback((l: DetailLevel) => { setLevel(l); writeDetailLevel(l); }, []);
  return [level, set];
}

/** First <=2 sentences of text, capped at maxChars. */
export function brief(text: string, maxChars = 280): { text: string; truncated: boolean } {
  const src = (text || "").replace(/\s+/g, " ").trim();
  const sentences = src.match(/[^.!?]+(?:[.!?]+(?=\s|$)|$)/g) || [src];
  let out = sentences.slice(0, 2).map((s) => s.trim()).join(" ");
  if (out.length > maxChars) out = out.slice(0, maxChars - 1).trimEnd() + "…";
  return { text: out, truncated: out.length < src.length };
}

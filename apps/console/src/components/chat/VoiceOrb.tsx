"use client";

import { useEffect, useRef, type ReactNode } from "react";
import type { VoicePhase } from "@/lib/voice";
import styles from "./VoiceOrb.module.css";

export function orbState(phase: VoicePhase, level: number) {
  return { phase, level: Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0 };
}

/** One audio-level loop drives both the orb and the transcript's bars. */
export function VoiceOrb({ phase, getLevel, children }: { phase: VoicePhase; getLevel: () => number; children: ReactNode }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    const stop = () => { cancelAnimationFrame(frame); root.current?.style.setProperty("--lvl", "0"); };
    const loop = () => {
      root.current?.style.setProperty("--lvl", orbState(phase, getLevel()).level.toFixed(2));
      frame = requestAnimationFrame(loop);
    };
    const update = () => { stop(); if (!motion.matches && phase !== "idle") frame = requestAnimationFrame(loop); };
    update(); motion.addEventListener("change", update);
    return () => { stop(); motion.removeEventListener("change", update); };
  }, [getLevel, phase]);
  return <div ref={root} className={styles.root} data-voice-phase={phase}>
    {phase !== "idle" && <div className={`${styles.orb} ${styles[phase]}`} data-voice-orb aria-hidden="true" />}
    {children}
  </div>;
}

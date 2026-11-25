"use client";

import { useState } from "react";
import { emergencyStop, SHORTCUT_LABEL } from "@/lib/emergencyStop";
import { useComputerUseActive } from "@/lib/useComputerUseActive";
import styles from "./EmergencyStopButton.module.css";

/** Red Stop for any running computer-use / browser task. Renders nothing otherwise. */
export function EmergencyStopButton({ className }: { className?: string }) {
  const active = useComputerUseActive();
  const [state, setState] = useState<"idle" | "stopping" | "failed">("idle");
  if (!active && state === "idle") return null;
  return <button type="button" className={`${styles.stop} ${className || ""}`} data-usage-control="emergency_stop"
    title={`Stop computer use now (${SHORTCUT_LABEL})`} aria-label="Stop computer use" disabled={state === "stopping"}
    onClick={async () => {
      setState("stopping");
      try { await emergencyStop("app"); setState("idle"); } catch { setState("failed"); }
    }}>{state === "stopping" ? "Stopping…" : state === "failed" ? "Stop failed, try again" : "Stop"}</button>;
}

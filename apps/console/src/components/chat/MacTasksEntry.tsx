"use client";

import { useEffect, useRef, useState } from "react";
import { api, verifiedSessionEpoch } from "@/lib/client";
import { isDesktopShell } from "@/lib/desktop";
import { useFeatureVisibility, useLocalMode } from "@/components/layout/LocalModeBoundary";
import { SUPPORTED_MAC_TASK_EXAMPLES } from "@/lib/reviewableDeviceRequest";
import { desktopExecutionAvailability, desktopExecutionMessage, type DesktopExecutionAvailability } from "@/lib/desktopExecutionAvailability";
import styles from "./MacTasksEntry.module.css";

export function MacTasksEntry({
  ventureId,
  onSelect,
  compact = false,
}: {
  ventureId: string;
  onSelect?: (text: string) => void;
  compact?: boolean;
}) {
  const local = useLocalMode();
  const visible = useFeatureVisibility()("settings", "mac");
  const desktop = isDesktopShell();
  const epoch = verifiedSessionEpoch();
  const ownerRef = useRef({ ventureId, epoch });
  if (ownerRef.current.ventureId !== ventureId || ownerRef.current.epoch !== epoch) ownerRef.current = { ventureId, epoch };
  const renderedOwner = ownerRef.current;
  const [receipt, setReceipt] = useState<{ owner: typeof renderedOwner; status: DesktopExecutionAvailability } | null>(null);
  const availability = receipt?.owner === renderedOwner ? receipt.status : "unknown";
  useEffect(() => {
    if (!desktop || !visible) return;
    let active = true;
    let serial = 0;
    const refresh = () => {
      const generation = ++serial;
      setReceipt(null);
      void api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`).then((result) => {
      if (active && generation === serial && renderedOwner === ownerRef.current && renderedOwner.epoch === verifiedSessionEpoch()) {
        setReceipt({ owner: renderedOwner, status: desktopExecutionAvailability(result) });
      }
    });
    };
    refresh();
    window.addEventListener("supymem-desktop-pause-changed", refresh);
    window.addEventListener("supymem-session-changed", refresh);
    return () => { active = false; serial++; window.removeEventListener("supymem-desktop-pause-changed", refresh); window.removeEventListener("supymem-session-changed", refresh); };
  }, [desktop, ventureId, renderedOwner, visible]);

  if (!visible) return null;

  if (!desktop) return (
    <section className={compact ? styles.compact : styles.entry} aria-label="Mac tasks">
      <h3 className={styles.title}>Mac tasks</h3>
      <p>{local ? "Your browser cannot control apps on your machine. Ask for manual steps in chat, or open your local desktop app to use your permitted tools." : "Open this venture in Supymem for Mac to prepare, review or run local tasks. This browser cannot access the local executor."}</p>
    </section>
  );

  return (
    <section className={compact ? styles.compact : styles.entry} aria-label="Mac tasks">
      <div className={styles.heading}>
        <h3 className={styles.title}>Mac tasks</h3>
        <p className={styles.lede}>
          {local ? "You can ask for a task on your machine. Your saved permissions still apply. Choose a task below or describe what you want in chat." : "Reviewed actions on this Mac. Each one opens a separate review before it runs."}
        </p>
      </div>
      <div className={styles.examples} role="list">
        {SUPPORTED_MAC_TASK_EXAMPLES.filter((example) => !("desktop" in example) || availability === "available").map((example) => (
          <button
            key={example.label}
            type="button"
            className={styles.example}
            role="listitem"
            onClick={() => onSelect?.(example.text)}
            title={example.text}
          >
            {example.label}
          </button>
        ))}
      </div>
      <p className={styles.foot}>
        {availability !== "available" ? desktopExecutionMessage(availability) : null}
        {onSelect && availability === "available"
          ? local ? "You can also describe a task for an installed app in chat. Your saved permissions still apply." : "Or describe a task for an installed Mac app in chat. Supymem will show the selected app, window, and permitted actions for review before it runs."
          : null}
        {" "}
        </p>
    </section>
  );
}

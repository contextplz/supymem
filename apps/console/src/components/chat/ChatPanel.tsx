"use client";

import { useCallback, useContext, useEffect, useMemo, useRef, type ComponentProps, type ReactNode } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { cx } from "@/lib/cx";
import { renderLightMd } from "@/lib/chatMarkdown";
import styles from "./ChatDock.module.css";

export function ChatPanel({ panel = true, className, ...props }: ComponentProps<"div"> & { panel?: boolean }) {
  const home = useContext(FounderUIContext) === "home";
  return <div {...props} className={cx(styles.dock, panel && styles.panel, home && panel && styles.homePanel, className)} />;
}

export function ChatMessageRow({ own, portrait, children, ...props }: ComponentProps<"div"> & { own?: boolean; portrait?: ReactNode }) {
  return <div {...props} className={cx(styles.msg, styles.msgRow, own && styles.msgOwn)}>
    {!own && portrait}
    <div className={styles.imStack}>{children}</div>
  </div>;
}

export function ChatWorking({ waitingLonger = false }: { waitingLonger?: boolean } = {}) {
  return <div className={styles.sendingRow} role="status" aria-label="Charlie is working">
    <span>{waitingLonger ? "Thinking a little longer…" : "Charlie is working"}</span>
    <span className={styles.thinking} aria-hidden="true"><span className={styles.dot} /><span className={styles.dot} /><span className={styles.dot} /></span>
  </div>;
}

export function ChatMessageText({ text, ...props }: ComponentProps<"div"> & { text: string }) {
  // React rewrites innerHTML when its wrapper changes, even when the text is unchanged.
  const html = useMemo(() => ({ __html: renderLightMd(text) }), [text]);
  return <div {...props} dangerouslySetInnerHTML={html} />;
}

export function useChatSelection() {
  const pointerDown = useRef(false);
  useEffect(() => {
    const start = (event: PointerEvent) => { if (event.button === 0) pointerDown.current = true; };
    const stop = () => { pointerDown.current = false; };
    document.addEventListener("pointerdown", start, true);
    window.addEventListener("pointerup", stop, true);
    window.addEventListener("pointercancel", stop, true);
    window.addEventListener("blur", stop);
    return () => {
      document.removeEventListener("pointerdown", start, true);
      window.removeEventListener("pointerup", stop, true);
      window.removeEventListener("pointercancel", stop, true);
      window.removeEventListener("blur", stop);
    };
  }, []);
  return useCallback(() => pointerDown.current || (typeof window !== "undefined" && window.getSelection?.()?.isCollapsed === false), []);
}

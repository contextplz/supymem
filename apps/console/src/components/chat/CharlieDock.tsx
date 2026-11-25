"use client";

import Link from "next/link";
import { useContext, useEffect, useRef } from "react";
import { FounderUIContext } from "@/lib/founderUI";
import { getCharlieMascotState, type CharlieState } from "@/lib/charlieMascot";
import styles from "./CharlieDock.module.css";

export function CharlieFace({ small = false }: { small?: boolean }) {
  return (
    <span className={small ? styles.faceSmall : styles.face} aria-hidden="true">
      <img src="/charlie/charlie-mark.png" alt="" onError={event => { event.currentTarget.style.display = "none"; }} />
    </span>
  );
}

export function CharlieMascot({ small = false, active = true, className = "" }: { small?: boolean; active?: boolean; className?: string }) {
  const host = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!active || !host.current) return;
    let disposed = false;
    let mounted: Awaited<ReturnType<typeof import("@/lib/mascotPlayer.js").mountMascot>> | undefined;
    let { state, level } = getCharlieMascotState();
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ state: CharlieState; level: number | null }>).detail;
      state = detail.state; level = detail.level; mounted?.setState(state, level);
    };
    const target = host.current;
    window.addEventListener("supymem-charlie-mascot", update);
    void import("@/lib/mascotPlayer.js").then(({ mountMascot }) => {
      if (disposed) return;
      return mountMascot(target, false, small ? 34 : 48).then(player => {
        if (disposed) player.dispose();
        else { mounted = player; mounted.setState(state, level); }
      });
    }).catch(() => {});
    return () => { disposed = true; window.removeEventListener("supymem-charlie-mascot", update); mounted?.dispose(); };
  }, [active, small]);
  return (
    <span ref={host} className={`${small ? styles.mascotSmall : styles.mascot} ${className}`} aria-hidden="true">
      <img src="/charlie/poster.png" alt="" onError={event => { event.currentTarget.style.display = "none"; }} />
    </span>
  );
}

export function CharlieDock({
  managerName,
  open,
  chatAvailable,
  onOpen,
  onClose,
  local = false,
}: {
  managerName: string;
  open: boolean;
  chatAvailable: boolean;
  onOpen: () => void;
  onClose: () => void;
  local?: boolean;
}) {
  const home = useContext(FounderUIContext) === "home";
  if (open && chatAvailable) return null;

  if (open) {
    return (
      <aside id={home ? "supymem-chat-panel" : undefined} className={`${styles.emptyPanel}${home ? ` ${styles.homeEmptyPanel}` : ""}`} aria-label={`${managerName} chat`}>
        <div className={styles.emptyHeader}>
          <span className={styles.identity}>
            {local ? <CharlieMascot small active={false} /> : <CharlieFace small />}
            <span>
              <strong>{managerName}</strong>
              <small>Your company manager</small>
            </span>
          </span>
          <button type="button" className={styles.close} onClick={onClose} aria-label={`Dock ${managerName}`}>
            ×
          </button>
        </div>
        <div className={styles.emptyBody}>
          {local ? <CharlieMascot /> : <CharlieFace />}
          <strong>Choose a venture to start</strong>
          <p>I’ll be here whenever you need me.</p>
          <Link href="/" onClick={onClose}>Choose a venture</Link>
          <Link href="/new" onClick={onClose}>Start a new venture</Link>
        </div>
      </aside>
    );
  }

  return (
    <button
      type="button"
      className={`${styles.dock}${home ? ` ${styles.homeDock}` : ""}${local ? ` ${styles.localDock}` : ""}`}
      onClick={onOpen}
      aria-label={`Open chat with ${managerName}`}
      aria-expanded={false}
      aria-controls="supymem-chat-panel"
      title={`Chat with ${managerName}`}
    >
      {local ? <CharlieMascot /> : <CharlieFace />}
      <span className={styles.label}>{managerName}</span>
    </button>
  );
}

"use client";

import { useContext, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import { desktopInvoke, desktopListen, isDesktopShell } from "@/lib/desktop";
import { verifiedSessionSubject, verifiedSessionEpoch } from "@/lib/client";
import { isOtherVentureNotice, trustedDesktopNoticeTarget } from "@/lib/desktopNoticeTarget";
import { inChatActions } from "@/lib/chatRouteAction";
import { FounderUIContext } from "@/lib/founderUI";
import type { ChatMessage } from "@/lib/api-types";
import { parseEmployeeStamp } from "@/components/office/avatars";
import styles from "./DesktopNoticeBanner.module.css";

type Notice = {
  id: string;
  title: string;
  body: string;
  target_path?: string | null;
  subject: string; device_id: string; auth_epoch: number;
};

/** Frontmost equivalent of the macOS notice: shows when the shell emits desktop-notice. */
export function DesktopNoticeBanner({ ventureId, otherVentureTarget, messages }: { ventureId?: string | null; otherVentureTarget?: string; messages?: ChatMessage[] } = {}) {
  const router = useRouter();
  const pathname = usePathname();
  const home = useContext(FounderUIContext) === "home";
  const [notice, setNotice] = useState<Notice | null>(null);
  const href = trustedDesktopNoticeTarget(notice?.target_path);
  const alreadyInChat = Boolean(notice && href && !isOtherVentureNotice(href, pathname, ventureId) && notice.body.trim() && messages?.some(message => {
    if (message.role !== "assistant") return false;
    const text = parseEmployeeStamp(message.text || message.body_md || "").text.trim();
    return text === notice.body.trim() || Array.from(text).slice(0, 400).join("").trim() === notice.body.trim();
  }));

  useEffect(() => {
    if (!isDesktopShell()) return;
    let stop: () => void = () => undefined;
    let closed = false;
    const retire = () => setNotice(null);
    window.addEventListener("supymem-session-changed", retire);
    void desktopListen("desktop-notice", async (payload) => {
      const value = payload as Notice | null;
      if (closed || !value || typeof value.id !== "string" || typeof value.title !== "string") return;
      const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
      if (!actor || value.subject !== actor || typeof value.device_id !== "string" ||
          !Number.isSafeInteger(value.auth_epoch) || value.auth_epoch < 0) return;
      const current = await desktopInvoke<{subject: string; device_id: string; auth_epoch: number}>("desktop_notice_custody");
      if (closed || actor !== verifiedSessionSubject() || epoch !== verifiedSessionEpoch() || !current ||
          current.subject !== value.subject || current.device_id !== value.device_id || current.auth_epoch !== value.auth_epoch) return;
      setNotice({
        subject: value.subject, device_id: value.device_id, auth_epoch: value.auth_epoch,
        id: value.id,
        title: value.title,
        body: typeof value.body === "string" ? value.body : "",
        target_path: typeof value.target_path === "string" ? value.target_path : null,
      });
    }).then((unsub) => { if (closed) unsub(); else stop = unsub; });
    return () => { closed = true; stop(); window.removeEventListener("supymem-session-changed", retire); };
  }, []);

  useEffect(() => { setNotice(null); }, [pathname]);

  // Native event emission alone is not proof that this banner was rendered.
  // This effect runs after its DOM commit; the native command independently
  // rechecks the exact account/device/generation before recording the channel.
  useEffect(() => {
    const actor = verifiedSessionSubject(), epoch = verifiedSessionEpoch();
    if (!notice || alreadyInChat || !actor || notice.subject !== actor) return;
    let retired = false;
    const retire = () => { retired = true; };
    window.addEventListener("supymem-session-changed", retire);
    if (!retired && actor === verifiedSessionSubject() && epoch === verifiedSessionEpoch()) {
      void desktopInvoke("desktop_notice_presented", {noticeId: notice.id, subject: notice.subject,
        deviceId: notice.device_id, authEpoch: notice.auth_epoch});
    }
    return () => { retired = true; window.removeEventListener("supymem-session-changed", retire); };
  }, [notice, alreadyInChat]);

  if (alreadyInChat) return null;
  if (!notice) {
    if (otherVentureTarget) return null;
    return ventureId || pathname === "/new" ? <div className={styles.other} aria-hidden="true" /> : null;
  }
  const chatAction = home && href ? inChatActions([{ label: "Open review", href }])[0] : null;
  const otherVenture = isOtherVentureNotice(notice.target_path, pathname, ventureId);
  // Another venture's blocker never covers this venture's workflow: only a small link to the global inbox.
  if (otherVenture) {
    const target = otherVentureTarget && typeof document !== "undefined" ? document.getElementById(otherVentureTarget) : null;
    const line = <aside className={`${styles.other}${target ? ` ${styles.railOther}` : ""}`} role="status" aria-live="polite">
      <button type="button" onClick={() => { setNotice(null); router.push("/"); }}>1 other venture needs you</button>
      {target && <span aria-hidden="true">·</span>}
      <button type="button" className="ghost sm" onClick={() => setNotice(null)} aria-label="Dismiss">Dismiss</button>
    </aside>;
    return target ? createPortal(line, target) : line;
  }
  const body = notice.body.replace(/\s*\((?:https?:\/\/\S+|\/\S*)\)/g, "").trim();
  const [intro, ...steps] = body.split(/\s*\d+\)\s+/);
  const stepList = <ol>{steps.map((step, index) => <li key={index}>{step}</li>)}</ol>;
  return (
    <aside className={`${styles.banner}${home ? ` ${styles.compact}` : ""}`} role="status" aria-live="polite">
      <div className={styles.copy}>
        <strong>{notice.title}</strong>
        {!home && steps.length ? <>
          {intro && <p>{intro}</p>}
          {steps.length > 3 ? <details>
            <summary data-usage-control="desktop_notice_steps">View all {steps.length} steps</summary>
            {stepList}
          </details> : stepList}
        </> : body ? <p>{body}</p> : null}
      </div>
      <div className={styles.actions}>
        {href ? (
          <button
            type="button"
            className="primary sm"
            onClick={() => {
              setNotice(null);
              if (chatAction?.action) {
                const [, , destination, review] = href.split("/");
                router.push(`/v/${destination}?chat=1&notice_review=${review}`);
              } else router.push(href);
            }}
          >
            {chatAction?.action ? "Open in chat" : "Open review"}
          </button>
        ) : null}
        <button type="button" className="ghost sm" onClick={() => setNotice(null)}>
          Dismiss
        </button>
      </div>
    </aside>
  );
}

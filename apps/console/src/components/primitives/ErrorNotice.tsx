"use client";

import { createElement, useEffect, type CSSProperties, type ReactNode } from "react";
import { reportUserError } from "@/lib/userError";
import { AI_SETTINGS_HREF, BUY_MORE_HREF, LOCAL_USAGE_LIMIT_MESSAGE, USAGE_LIMIT_MESSAGE, usageLimitMessage } from "@/lib/credits";
import { useLocalMode } from "@/components/layout/LocalModeBoundary";

/** Report an error string the moment it is shown (repeats within 5s are dropped by the reporter). */
export function useReportedError(error: string | null | undefined, source: string, intent?: string) {
  useEffect(() => { if (error) reportUserError({ message: error, source, intent }); }, [error, source, intent]);
}

/** The one way to render a user-visible error: role="alert", reported on display. */
export function ErrorNotice({ error, source, intent, as = "p", id, className, style, children }: {
  id?: string; error: string | null | undefined; source: string; intent?: string;
  as?: "p" | "div" | "span" | "section" | "figcaption"; className?: string; style?: CSSProperties; children?: ReactNode;
}) {
  useReportedError(error, source, intent);
  const local = useLocalMode();
  if (!error) return null;
  const limit = error === USAGE_LIMIT_MESSAGE || error === LOCAL_USAGE_LIMIT_MESSAGE;
  return createElement(as, { role: "alert", "data-error-notice": "", "data-error-source": source, "data-intent": intent, id, className, style },
    limit ? usageLimitMessage(local) : children ?? error, limit ? createElement("a", { href: local ? AI_SETTINGS_HREF : BUY_MORE_HREF }, local ? " Open your AI service settings" : " Buy more") : null);
}

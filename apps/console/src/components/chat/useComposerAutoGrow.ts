"use client";

import { useCallback, useLayoutEffect, useRef } from "react";
import { COMPOSER_LINE_PX, COMPOSER_PAD_PX, composerHeightPx } from "@/lib/chatComposer";

/** Keep a textarea height synced to content up to the shared max. */
export function useComposerAutoGrow(
  value: string,
  dockHeight?: number,
  lineHeight = COMPOSER_LINE_PX,
  padding = COMPOSER_PAD_PX,
): { ref: React.RefObject<HTMLTextAreaElement | null>; sync: () => void } {
  const ref = useRef<HTMLTextAreaElement | null>(null);

  const sync = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const { height } = composerHeightPx({
      scrollHeight: el.scrollHeight,
      lineHeight,
      padding,
      dockHeight,
    });
    el.style.height = `${height}px`;
    el.style.overflowY = el.scrollHeight > height + 1 ? "auto" : "hidden";
  }, [dockHeight, lineHeight, padding]);

  useLayoutEffect(() => {
    sync();
  }, [value, sync]);

  return { ref, sync };
}

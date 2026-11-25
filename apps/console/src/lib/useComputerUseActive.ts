"use client";

import { useEffect, useState } from "react";
import { stopButtonVisible, watchComputerUse } from "./emergencyStop.ts";
import { isDesktopShell } from "./desktop.ts";

/** True only in the Mac app while a computer-use / browser device task is running. */
export function useComputerUseActive(): boolean {
  const [active, setActive] = useState(false);
  useEffect(() => {
    let off: (() => void) | undefined;
    let gone = false;
    void watchComputerUse(setActive).then((unlisten) => { if (gone) unlisten(); else off = unlisten; });
    return () => { gone = true; off?.(); };
  }, []);
  return stopButtonVisible(isDesktopShell(), active);
}

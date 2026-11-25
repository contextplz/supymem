"use client";

import { useEffect, useState } from "react";
import { ensureSession, type SessionInfo } from "@/lib/client";

/** Passphrase sessions are operator sessions; founders sign in with an account. */
export const isOperatorMode = (mode?: string) => Boolean(mode) && mode !== "account";

/** False until the session says otherwise, so founders never see operator tools flash. */
export function useIsOperator(): boolean {
  const [operator, setOperator] = useState(false);
  useEffect(() => {
    let live = true;
    void ensureSession().then((r) => { if (live) setOperator(r.ok && isOperatorMode((r.data as SessionInfo | null)?.mode)); });
    return () => { live = false; };
  }, []);
  return operator;
}

"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { LocalProviderGate } from "@/app/setup/LocalSetup";
import { isHostedOnlyPath, isLocalConsole, localHomeHref } from "@/lib/localMode";
import { appRouteVentureId, forgetAppRoute } from "@/lib/appResume";
import { api, ensureSession, verifiedSessionSubject } from "@/lib/client";
import { surfaceCapabilities, surfaceVisible, type SurfaceCapability } from "@/lib/localFeatureVisibility";
export { isHostedOnlyPath } from "@/lib/localMode";

const LocalModeContext = createContext(false);
const LocalCapabilitiesContext = createContext<SurfaceCapability[] | null>(null);
const CAPABILITY_LOAD_ERROR = "Your features could not be loaded. Select Try again, or open Settings > Keys and connections to check your setup.";

export function useLocalMode(): boolean {
  return useContext(LocalModeContext);
}

export function useFeatureVisibility() {
  const local = useLocalMode();
  const capabilities = useContext(LocalCapabilitiesContext);
  return (surface: string, value: string) => surfaceVisible(capabilities, surface, value, local);
}

export function LocalVentureGate({ children, ventureId, blocking = true }: { children: ReactNode; ventureId: string | null; blocking?: boolean }) {
  const router = useRouter();
  const [checked, setChecked] = useState<boolean | "missing">(false);
  useEffect(() => {
    if (!ventureId) return;
    let active = true;
    void (async () => {
      const session = await ensureSession();
      if (!active) return;
      if (session.ok) {
        const result = await api(`/api/ventures/${encodeURIComponent(ventureId)}`);
        if (!active) return;
        if (result.status === 404) {
          setChecked("missing");
          try { forgetAppRoute(window.localStorage, verifiedSessionSubject(), ventureId); } catch {}
          router.replace("/");
          return;
        }
      }
      setChecked(true);
    })().catch(() => { if (active) setChecked(true); });
    return () => { active = false; };
  }, [ventureId, router]);
  return !ventureId || checked !== "missing" && (!blocking || checked) ? children : <main role="status" data-webview-pending="venture_load">Opening your venture. Please wait.</main>;
}

export function LocalModeBoundary({ children, local }: { children: ReactNode; local: boolean }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const mode = local || isLocalConsole();
  const [capabilities, setCapabilities] = useState<SurfaceCapability[] | null>(null);
  const [capabilityError, setCapabilityError] = useState("");
  const [capabilityAttempt, setCapabilityAttempt] = useState(0);
  useEffect(() => {
    if (!mode) return;
    let active = true;
    void ensureSession().then(session => active && session.ok ? api("/api/local/capabilities") : null).then(result => {
      if (!active) return;
      const values = result?.ok ? surfaceCapabilities((result.data as { capabilities?: unknown })?.capabilities) : null;
      if (values) setCapabilities(values);
      else setCapabilityError(CAPABILITY_LOAD_ERROR);
    }).catch(() => {
      if (active) setCapabilityError(CAPABILITY_LOAD_ERROR);
    });
    return () => { active = false; };
  }, [mode, capabilityAttempt]);
  const hidden = mode && (isHostedOnlyPath(pathname) || capabilities !== null && !surfaceVisible(capabilities, "paths", pathname, true));
  const home = localHomeHref(pathname, searchParams.get("venture"));
  const destination = /^\/v\/[^/]+\/settings(?:\/|$)/.test(pathname) ? "/settings/local" : home;
  const ventureId = appRouteVentureId(`${pathname}?${searchParams.toString()}`);
  const ventureHome = /^\/v\/[^/]+\/?$/.test(pathname);
  const workspaceHome = pathname === "/" || ventureHome;
  useEffect(() => {
    if (hidden) router.replace(destination);
  }, [hidden, destination, router]);
  const notice = <p role="status">Open your venture to continue your work, or <a href="/settings/local">open Settings &gt; Keys and connections</a> to manage your local app.</p>;
  const featureRetry = capabilityError && <><button type="button" onClick={() => { setCapabilityError(""); setCapabilityAttempt(current => current + 1); }}>Try again</button>{" "}<a href="/settings/local">Open your keys and connections</a></>;
  return <LocalModeContext.Provider value={mode}><LocalCapabilitiesContext.Provider value={capabilities}>{hidden
    ? <main>{notice}<a href={destination}>{destination === home ? "Open your venture home" : "Open your keys and connections"}</a></main>
    : mode && !capabilities && !workspaceHome && !pathname.startsWith("/settings/local") && !pathname.startsWith("/setup")
      ? <main data-webview-pending={capabilityError ? undefined : "local_capabilities"}><p role="status">{capabilityError || "Opening your features. Please wait."}</p>{featureRetry}</main>
    : mode ? <>{workspaceHome && capabilityError && <div><p role="status">{capabilityError}</p>{featureRetry}</div>}
      <LocalProviderGate><LocalVentureGate key={ventureId} ventureId={ventureId} blocking={!ventureHome}>{children}</LocalVentureGate></LocalProviderGate></>
    : children}</LocalCapabilitiesContext.Provider></LocalModeContext.Provider>;
}

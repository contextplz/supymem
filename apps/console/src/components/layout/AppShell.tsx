"use client";

import { inviteAfterAuthentication } from "@/lib/onboardingInvite";
import Link from "@/components/chat/ChatLink";
import { PasswordResetForm } from "@/components/auth/PasswordResetForm";
import { captureFounderTimezone } from "@/lib/taskApi";
import { inviteErrorMessage, normalizeInviteCode, recallInvite, rememberInvite } from "@/lib/inviteCode";
import type { CSSProperties, FormEvent, ReactNode } from "react";
import { Suspense, createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { MAC_TASK_PERMISSION_EVENT, type MacTaskPermissionRequest } from "@/lib/macPermissions";
import { DesktopTaskPermissionCard } from "@/components/desktop/DesktopPermissionSetup";
import { shouldResetPortfolio } from "@/lib/nativeAuthTransition";
import {
  api,
  ensureSession,
  isInvalidSession,
  login,
  loginAccount,
  signup,
  startCognitoLogin,
  resendVerification,
  logout,
  resolveGateway,
  verifiedSessionSubject,
  verifiedSessionEpoch,
  clearVerifiedSession,
} from "@/lib/client";
import { desktopInvoke, desktopInvokeQuiet, desktopListen, isDesktopShell } from "@/lib/desktop";
import { DesktopAccountConnection, type NativeAuthStatus } from "@/components/desktop/DesktopAccountConnection";
import { nativeAuthActionPending, nativeAuthAvailabilityMessage } from "@/lib/nativeAuthAvailability";
import { signInErrorMessage } from "@/lib/signInReturn";
import { nativeSessionNeedsSignIn } from "@/lib/nativeSessionGate";
import { continueDeviceApproval, readDeviceIntent } from "@/lib/deviceIntent";
import { PASSWORD_REQUIREMENTS, accountSignupError } from "@/lib/passwordPolicy";
import { teamCountLabel, ventureStatusLabel } from "@/lib/format";
import { cx } from "@/lib/cx";
import { AgentDesk } from "@/components/office/AgentDesk";
import { VentureChatLauncher, type PendingAsk } from "@/components/chat/VentureChatLauncher";
import { chatFirstHidden, isIdeaJourneyPath, showsDetailToggle } from "@/lib/chatVisibility";
import { HOME_NAVIGATION_EVENT, homeRouteAction, inChatActions, routeInChat } from "@/lib/chatRouteAction";
import { captureChatLink } from "@/components/chat/ChatLink";
import { trustedDesktopNoticeTarget } from "@/lib/desktopNoticeTarget";
import { usePersistedDockSize } from "@/components/chat/ChatDock";
import { ReportProblem } from "@/components/support/ReportProblem";
import { DesktopNoticeBanner } from "@/components/desktop/DesktopNoticeBanner";
import type { IdeaPageStatus } from "@/lib/runStatusPresentation";
import { OfficeProvider, useOfficeOptional } from "@/components/office/OfficeProvider";
import { OfficeTeamRail } from "@/components/office/OfficeTeamRail";
import { WorkspaceSwitcher } from "@/components/office/WorkspaceSwitcher";
import { OpsWorkspaceGate } from "@/components/ops/OpsWorkspaceGate";
import { LocalConnections } from "@/components/connections/LocalConnections";
import {
  isCompanyOperationsPath,
  shellVentureId,
  ventureQueryId,
  type OperationsScope,
} from "@/lib/operationsScope";
import { SideNav } from "./SideNav";
import { useLocalMode } from "./LocalModeBoundary";
import { SampleVentures } from "@/app/setup/LocalSetup";
import { GuideNavLink } from "@/components/guide/GuideNavLink";
import { NAV_ICONS } from "./NavIcons";
import { RunStatusBar } from "./RunStatusBar";
import { ASK_EVENT, ChatOpenContext, ClarificationSkipContext, NextStepStrip } from "./NextStepStrip";
import { PREFILL_ACTION } from "@/lib/chatPrefill";
import { FounderCoaching } from "@/components/planning/FounderCoaching";
import { UsageTracker } from "./UsageTracker";
import { AttentionToast } from "./AttentionToast";
import { listenForUsageSubject } from "@/lib/usageCollectorState";
import { WorkspaceErrorBoundary } from "./WorkspaceErrorBoundary";
import { AppShellErrorBoundary } from "./AppShellErrorBoundary";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";
import { DetailToggle } from "@/components/primitives/DetailToggle";
import { resumeAppRoute } from "@/lib/appResume";
import { FounderUIContext, useFounderUIState } from "@/lib/founderUI";
import { CompanyOffice } from "@/components/office/CompanyOffice";
import { CharlieDock, CharlieFace, CharlieMascot } from "@/components/chat/CharlieDock";
import { CompanySettingsDialog } from "@/components/office/CompanySettingsDialog";
import chatPanelStyles from "@/components/chat/VentureChatLauncher.module.css";
import chatDockStyles from "@/components/chat/ChatDock.module.css";


type LoginMode = "passphrase" | "account";

// Initial SSR and hydration agree on false. Once the renderer has mounted, a
// client-side route can retain the native shell without a one-frame web portal.
let observedDesktopShell = false;
let resumeNavigationChecked = false;
let localChatDraft: { ventureId: string; subject: string; epoch: number; detail: PendingAsk } | null = null;

export type AppShellProps = {
  children: ReactNode;
  title?: string;
  subtitle?: string;
  stage?: string;
  status?: { code?: string; label?: string; detail?: string } | null;
  ventureId?: string;
  ideaStatus?: IdeaPageStatus;
  clarificationSkip?: { skip: () => Promise<boolean>; disabled: boolean } | null;
  headerRight?: ReactNode;
  bottomBar?: ReactNode;
  chat?: ReactNode;
  showChat?: boolean;
  chatCollapsed?: boolean;
  onChatCollapsedChange?: (collapsed: boolean) => void;
  preferChatCollapsed?: boolean;
  fill?: boolean;
  bare?: boolean;
  officeHome?: boolean;
};

type PageChrome = Omit<AppShellProps, "children" | "ventureId">;
const PageChromeContext = createContext<((chrome: PageChrome) => void) | null>(null);
// useLayoutEffect warns during SSR; fall back to useEffect there.
const useIsoLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

/**
 * Mounted once in a route layout. Pages keep rendering <AppShell …>; inside this
 * provider that call only reports the page's title/actions/etc. up to the single
 * persistent shell and renders its children, so navigation never remounts the shell.
 */
export function PersistentAppShell({ ventureId, children }: { ventureId?: string; children: ReactNode }) {
  const [chrome, setChrome] = useState<PageChrome>({});
  return (
    <PageChromeContext.Provider value={setChrome}>
      <AppShellFrame ventureId={ventureId} {...chrome}>{children}</AppShellFrame>
    </PageChromeContext.Provider>
  );
}

export function AppShell(props: AppShellProps) {
  const setChrome = useContext(PageChromeContext);
  const { children, ventureId: _ventureId, ...chrome } = props;
  useIsoLayoutEffect(() => {
    setChrome?.(chrome);
  });
  useIsoLayoutEffect(() => () => setChrome?.({}), [setChrome]);
  return setChrome ? <>{children}</> : <AppShellFrame {...props} />;
}

function AppShellFrame({
  children,
  title,
  subtitle,
  stage,
  status,
  ventureId,
  ideaStatus,
  clarificationSkip,
  headerRight,
  bottomBar,
  fill,
  bare,
  officeHome,
}: AppShellProps) {
  const pathname = usePathname();
  const local = useLocalMode();
  const [accountKey, setAccountKey] = useState(0);
  const accountSubjectRef = useRef(verifiedSessionSubject());
  const [permissionRequest, setPermissionRequest] = useState<MacTaskPermissionRequest | null>(null);
  const [permissionRequestKey, setPermissionRequestKey] = useState(0);
  const permissionRequestRef = useRef<MacTaskPermissionRequest | null>(null);
  useIsoLayoutEffect(() => {
    permissionRequestRef.current = null;
    setPermissionRequest(null);
    const changed = (event: Event) => {
      const subject = (event as CustomEvent<{ subject?: string | null }>).detail?.subject;
      if (!subject || (accountSubjectRef.current && accountSubjectRef.current !== subject)) {
        permissionRequestRef.current = null;
        setPermissionRequest(null);
      }
      if (accountSubjectRef.current && accountSubjectRef.current !== subject) setAccountKey(key => key + 1);
      accountSubjectRef.current = subject || null;
    };
    const needed = (event: Event) => {
      const request = (event as CustomEvent<MacTaskPermissionRequest>).detail;
      if (local || !isDesktopShell() || typeof request?.resume !== "function") return;
      permissionRequestRef.current = request;
      setPermissionRequest(request);
      setPermissionRequestKey(key => key + 1);
    };
    window.addEventListener("supymem-session-changed", changed);
    window.addEventListener(MAC_TASK_PERMISSION_EVENT, needed);
    return () => {
      permissionRequestRef.current = null;
      window.removeEventListener("supymem-session-changed", changed);
      window.removeEventListener(MAC_TASK_PERMISSION_EVENT, needed);
    };
  }, [local, pathname]);
  async function resumeMacTask() {
    if (!permissionRequest || permissionRequestRef.current !== permissionRequest) return;
    permissionRequestRef.current = null;
    setPermissionRequest(null);
    await permissionRequest.resume();
  }
  return (
    <OfficeProvider key={accountKey} ventureId={ventureId}>
      <Suspense fallback={<div className="page-loading" role="status" data-webview-pending="route_load" aria-live="polite"><span>Loading your workspace...</span></div>}>
        <AppShellInner
          title={title}
          subtitle={subtitle}
          stage={stage}
          status={status}
          ventureId={ventureId}
          ideaStatus={ideaStatus}
          clarificationSkip={clarificationSkip}
          headerRight={headerRight}
          bottomBar={bottomBar}
          fill={fill}
          bare={bare}
          officeHome={officeHome}
          permissionRequest={permissionRequest}
          permissionRequestKey={permissionRequestKey}
          onMacTaskPermissionsGranted={resumeMacTask}
        >
          {children}
        </AppShellInner>
      </Suspense>
    </OfficeProvider>
  );
}

function AppShellInner({
  children,
  title,
  subtitle,
  stage,
  status,
  ventureId,
  ideaStatus,
  clarificationSkip,
  headerRight,
  bottomBar,
  fill,
  bare,
  officeHome,
  permissionRequest,
  permissionRequestKey,
  onMacTaskPermissionsGranted,
}: {
  children: ReactNode;
  title?: string;
  subtitle?: string;
  stage?: string;
  status?: { code?: string; label?: string; detail?: string } | null;
  ventureId?: string;
  ideaStatus?: IdeaPageStatus;
  clarificationSkip?: { skip: () => Promise<boolean>; disabled: boolean } | null;
  headerRight?: ReactNode;
  bottomBar?: ReactNode;
  fill?: boolean;
  permissionRequest: MacTaskPermissionRequest | null;
  bare?: boolean;
  officeHome?: boolean;
  permissionRequestKey: number;
  onMacTaskPermissionsGranted: () => Promise<void>;
}) {
  const pathname = usePathname();
  const local = useLocalMode();
  if (local && pathname === "/") {
    children = <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      <div style={{ flex: "0 1 auto", minHeight: 0, overflow: "auto" }}><SampleVentures refresh={pathname} /></div>
      <div style={{ flex: "1 0 50%", minHeight: 0 }}>{children}</div>
    </div>;
  }
  useEffect(() => { if (!local && pathname !== "/auth/device") readDeviceIntent(); }, [local, pathname]);
  const operatorLogin = pathname === "/operator";
  const workspaceBodyRef = useRef<HTMLDivElement>(null);
  const localHomeRef = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();
  const office = useOfficeOptional();
  const [savedTitle, setSavedTitle] = useState<string | null>(null);
  // AppShell lives in each page. Client-side navigation remounts it, so reuse
  // only the verified subject from this renderer while the new check runs.
  // The authoritative auth event or check removes it on real sign-out.
  const [authed, setAuthed] = useState(() => Boolean(verifiedSessionSubject()));
  const [checking, setChecking] = useState(() => !verifiedSessionSubject());
  const [loginMode, setLoginMode] = useState<LoginMode>("passphrase");
  const [hostedWeb, setHostedWeb] = useState(false);
  useIsoLayoutEffect(() => {
    if (workspaceBodyRef.current) workspaceBodyRef.current.scrollTop = 0;
  }, [pathname]);
  useEffect(() => {
    if (authed && operatorLogin) router.replace("/");
  }, [authed, operatorLogin, router]);
  const [nativeDesktop, setNativeDesktop] = useState(() => observedDesktopShell);
  const browserEntry = !local && !nativeDesktop && (pathname === "/auth" || pathname.startsWith("/auth/") || pathname === "/download" || pathname.startsWith("/invite/"));
  useEffect(() => {
    if (local && (pathname === "/auth" || pathname.startsWith("/auth/"))) router.replace("/");
  }, [local, pathname, router]);
  useIsoLayoutEffect(() => {
    if (permissionRequest && workspaceBodyRef.current) workspaceBodyRef.current.scrollTop = 0;
  }, [permissionRequest, permissionRequestKey, authed]);
  const [sessionSubject, setSessionSubject] = useState<string | undefined>(() => verifiedSessionSubject() || undefined);
  const { layout: configuredUI, loaded: configuredUILoaded } = useFounderUIState(sessionSubject, authed && !local);
  const founderUI = local ? "home" : configuredUI;
  const founderUILoaded = local || configuredUILoaded;
  useEffect(() => {
    if (browserEntry) return;
    const firstNavigation = !resumeNavigationChecked;
    if (pathname !== "/") resumeNavigationChecked = true;
    if (!authed || !founderUILoaded || !sessionSubject || verifiedSessionSubject() !== sessionSubject) return;
    resumeNavigationChecked = true;
    try {
      const location = window.location;
      const href = resumeAppRoute(window.localStorage, sessionSubject,
        `${location.pathname}${location.search}${location.hash}`, firstNavigation, founderUI, local);
      if (href && !routeInChat(href)) router.replace(href);
    } catch { /* Storage denial must not block the shell. */ }
  }, [authed, founderUI, founderUILoaded, sessionSubject, pathname, searchParams, router, browserEntry, local]);
  const [pass, setPass] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  // The invitation code only gates the installer download. Sign-in never asks for it;
  // a code from an invite link is still passed along so its redemption is recorded.
  useEffect(() => {
    if (local) return;
    try {
      const value = normalizeInviteCode(new URLSearchParams(window.location.search).get("invite") || "") || recallInvite();
      setInviteCode(value);
      if (value) { setLoginMode("account"); rememberInvite(value); }
    } catch { /* Sign-in still works for existing accounts. */ }
  }, [local]);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [verificationEmail, setVerificationEmail] = useState("");
  const [verificationMessage, setVerificationMessage] = useState("");
  const [connectionError, setConnectionError] = useState("");
  function reconnectInPlace() {
    if (chatShown) setChatPanelOpen(true);
    setSessionAttempt(attempt => attempt + 1);
  }
  const [nativeAccountState, setNativeAccountState] = useState<string | undefined>();
  const nativeAccountStateRef = useRef<string | undefined>(undefined);
  const lastConnectedSubjectRef = useRef<string | undefined>(undefined);
  const [sessionAttempt, setSessionAttempt] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [settingsOpen, updateSettingsOpen] = useState(false);
  function setSettingsOpen(value: boolean | ((open: boolean) => boolean)) {
    if (local && (typeof value === "function" ? value(settingsOpen) : value)) { router.push("/settings/local"); return; }
    updateSettingsOpen(value);
  }
  const [settingsSection, setSettingsSection] = useState<"account" | "mac" | "manager" | "venture">("venture");
  const [operationsScope, setOperationsScope] = useState<OperationsScope | null>(null);
  const [chatPanelOpen, setChatPanelOpen] = useState(false);
  const [sidebarPinned, setSidebarPinned] = useState(false);
  const [sidebarExpanded, setSidebarExpanded] = useState(false);
  const sidebarHover = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeSidebar = () => {
    if (sidebarHover.current) clearTimeout(sidebarHover.current);
    sidebarHover.current = null;
    setSidebarExpanded(false);
  };
  useEffect(() => closeSidebar, []);
  useEffect(() => {
    try { setSidebarPinned(localStorage.getItem("supymem.sidebar") === "pinned"); } catch { /* storage blocked */ }
  }, []);
  const togglePin = () => setSidebarPinned((v) => {
    try { localStorage.setItem("supymem.sidebar", v ? "rail" : "pinned"); } catch { /* storage blocked */ }
    return !v;
  });
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [chatPanelSize, setChatPanelSize] = usePersistedDockSize();
  // Keep the venture name in the header while a route's page has not reported its title yet.
  const [ventureName, setVentureName] = useState("");
  const chatToggleRef = useRef<HTMLButtonElement>(null);
  const shownTitle = savedTitle || title || ventureName;
  const companyOperations = isCompanyOperationsPath(pathname);
  const activeScope = companyOperations ? operationsScope : null;
  const queryVentureId = companyOperations ? ventureQueryId(searchParams) : undefined;
  const effectiveVentureId = shellVentureId({
    pathVentureId: ventureId,
    queryVentureId,
    scopeVentureId: activeScope?.ventureId,
  });
  // Pages that pass only `stage` would show "Draft" for a venture whose card says
  // "Landing page live"; fall back to the same server status the card uses.
  const [fetchedStatus, setFetchedStatus] = useState<{ id: string; status: NonNullable<typeof status> } | null>(null);
  useEffect(() => {
    if (status || !authed || !effectiveVentureId) return;
    let live = true;
    void api(`/api/ventures/${effectiveVentureId}`).then((r) => {
      const next = (r.data as { status?: typeof status } | null)?.status;
      if (live && r.ok && next) setFetchedStatus({ id: effectiveVentureId, status: next });
    });
    return () => { live = false; };
  }, [status, authed, effectiveVentureId]);
  const shownStatus = status ?? (fetchedStatus && fetchedStatus.id === effectiveVentureId ? fetchedStatus.status : null);
  const isVentureOverview = /^\/v\/[^/]+\/?$/.test(pathname || "");
  const entryPage = pathname === "/new";
  const guidePage = /^\/(?:guide|v\/[^/]+\/guide)\/?$/.test(pathname || "");
  const officeTab = founderUI === "home" && isVentureOverview && ["archives", "toolkit", "departments"].includes(searchParams.get("office") || "");
  const chatFirstHome = founderUI === "home" && (entryPage || isVentureOverview && !officeTab);
  // Chat-first: the middle pages keep their routes and code, but founders land in Charlie's chat (founder_ui "classic" still shows them).
  const hiddenPage = founderUILoaded && founderUI === "home" && authed && chatFirstHidden(pathname || "", local);
  const routeQuery = searchParams.toString();
  useEffect(() => {
    if (hiddenPage) router.replace(effectiveVentureId ? `/v/${encodeURIComponent(effectiveVentureId)}?chat=1&chat_route=${encodeURIComponent(`${pathname}${routeQuery ? `?${routeQuery}` : ""}`)}` : "/");
  }, [hiddenPage, effectiveVentureId, pathname, routeQuery, router]);
  useEffect(() => {
    if (!authed || !sessionSubject || !effectiveVentureId || verifiedSessionSubject() !== sessionSubject) return;
    void captureFounderTimezone(effectiveVentureId).catch(() => {});
  }, [authed, sessionSubject, effectiveVentureId]);
  const [managerName, setManagerName] = useState("Charlie");
  useEffect(() => {
    if (!effectiveVentureId || chatPanelOpen) return;
    let live = true;
    void api(`/api/ventures/${encodeURIComponent(effectiveVentureId)}`).then((r) => {
      const name = r.ok ? (r.data as { manager_name?: string }).manager_name : "";
      if (live && name) setManagerName(name);
    });
    return () => { live = false; };
  }, [effectiveVentureId, chatPanelOpen]);
  useEffect(() => {
    // Settings renames the manager without reloading; keep the launcher label current.
    const renamed = (event: Event) => {
      const name = (event as CustomEvent<string>).detail;
      if (name) setManagerName(name);
    };
    window.addEventListener("supymem-manager-renamed", renamed);
    return () => window.removeEventListener("supymem-manager-renamed", renamed);
  }, []);
  const chatFirstEntry = searchParams.get("chat") === "1";
  const localBuildActivity = local && /^\/v\/[^/]+\/device-work-orders\/[^/]+\/?$/.test(pathname);
  const localSubpage = local && !localBuildActivity && /^\/v\/[^/]+\/[^/]/.test(pathname);
  const chatAvailable = Boolean(effectiveVentureId && pathname !== "/new" && bottomBar == null);  // the idea journey has the launcher too (closed by default), so its retry/next-step buttons can reach chat
  // As in #1248, the chat stays mounted (hidden) under the Company, Tuesday Archives and Toolkits tabs.
  const chatShown = !localBuildActivity && !localSubpage && !(founderUI === "home" && pathname === "/connections") && (entryPage && founderUI === "home" || chatAvailable && (founderUI === "home" && !guidePage || chatPanelOpen || chatFirstHome || officeTab));
  useEffect(() => {
    if (!local || !authed || window.location.hash) return;
    localHomeRef.current?.querySelector<HTMLElement>('main[tabindex="-1"], [role="main"][tabindex="-1"]:not([hidden])')?.focus({ preventScroll: true });
  }, [local, authed, pathname, officeTab]);
  function openInChat(detail: PendingAsk) {
    if (localSubpage && detail.action === PREFILL_ACTION && effectiveVentureId && sessionSubject && verifiedSessionSubject() === sessionSubject) {
      localChatDraft = { ventureId: effectiveVentureId, subject: sessionSubject, epoch: verifiedSessionEpoch(), detail };
      router.push(`/v/${encodeURIComponent(effectiveVentureId)}`);
      return;
    }
    setPendingAsk(detail); setChatPanelOpen(true);
    if (founderUI === "home" && effectiveVentureId && !chatFirstHome) router.push(`/v/${encodeURIComponent(effectiveVentureId)}`);
  }

  useEffect(() => {
    if (!local || !chatFirstHome || !chatAvailable || !authed || !sessionSubject || !localChatDraft) return;
    const draft = localChatDraft;
    localChatDraft = null;
    if (draft.ventureId !== effectiveVentureId || draft.subject !== sessionSubject || draft.subject !== verifiedSessionSubject()
        || draft.epoch !== verifiedSessionEpoch()) return;
    setPendingAsk(draft.detail); setChatPanelOpen(true);
  }, [local, chatFirstHome, chatAvailable, authed, sessionSubject, effectiveVentureId]);

  useEffect(() => {
    if (chatFirstEntry && effectiveVentureId) setChatPanelOpen(true);
    else if (founderUI === "classic" && isIdeaJourneyPath(pathname)) setChatPanelOpen(false);
    else if (office?.mentionPrefix) setChatPanelOpen(true);
  }, [chatFirstEntry, effectiveVentureId, founderUI, office?.mentionPrefix, pathname]);

  useEffect(() => {
    if (founderUI !== "home" || !chatAvailable || !effectiveVentureId || !chatFirstEntry) return;
    const target = trustedDesktopNoticeTarget(`/v/${effectiveVentureId}/${searchParams.get("notice_review") || ""}`);
    const review = target ? inChatActions([{ label: "Open review", href: target }], true, effectiveVentureId)[0] : null;
    if (!review?.action) return;
    setPendingAsk({ action: review.action, payload: review.payload });
    setChatPanelOpen(true);
    router.replace(`/v/${encodeURIComponent(effectiveVentureId)}`);
  }, [founderUI, chatAvailable, chatFirstEntry, effectiveVentureId, searchParams, router]);

  useEffect(() => {
    const href = searchParams.get("chat_route");
    if (founderUI !== "home" || !chatAvailable || !effectiveVentureId || !href) return;
    let target;
    try { target = homeRouteAction(href, effectiveVentureId); } catch { return; }
    if (target?.sidebar === "view" && target.href) { router.replace(target.href); return; }
    if (target?.href) return;
    if (target?.sidebar === "settings") { setSettingsSection(target.section || "venture"); setSettingsOpen(true); }
    else if (target?.action) { setPendingAsk({ action: target.action, payload: target.payload }); setChatPanelOpen(true); }
    router.replace(`/v/${encodeURIComponent(effectiveVentureId)}${target?.sidebar === "toolkit" ? "?office=toolkit" : ""}`);
  }, [founderUI, chatAvailable, effectiveVentureId, searchParams, router]);

  useEffect(() => {
    const ask = (event: Event) => {
      const detail = (event as CustomEvent<PendingAsk>).detail;
      if (detail?.action && chatAvailable) openInChat(detail);
    };
    window.addEventListener(ASK_EVENT, ask);
    return () => window.removeEventListener(ASK_EVENT, ask);
  }, [chatAvailable, founderUI, effectiveVentureId, chatFirstHome, localSubpage, sessionSubject, router]);

  useEffect(() => {
    if (founderUI !== "home") return;
    const navigate = (event: Event) => {
      const target = (event as CustomEvent<ReturnType<typeof homeRouteAction>>).detail;
      if (target?.sidebar === "settings") { setSettingsSection(target.section || "venture"); setSettingsOpen(true); }
      else if (target?.sidebar === "toolkit" && effectiveVentureId) router.push(`/v/${encodeURIComponent(effectiveVentureId)}?office=toolkit`);
      else if (target?.href) router.push(target.href);
      else if (target?.action && chatAvailable) openInChat({ action: target.action, payload: target.payload });
    };
    window.addEventListener(HOME_NAVIGATION_EVENT, navigate);
    return () => window.removeEventListener(HOME_NAVIGATION_EVENT, navigate);
  }, [founderUI, effectiveVentureId, chatAvailable, chatFirstHome, router]);

  useEffect(() => {
    setSavedTitle(null);
  }, [ventureId]);

  useEffect(() => {
    if (!companyOperations && operationsScope) setOperationsScope(null);
  }, [companyOperations, operationsScope]);

  useEffect(() => {
    const loopback = (host: string) => ["localhost", "127.0.0.1", "[::1]"].includes(host);
    const hosted = window.__SUPYMEM__?.mode === "hosted"
      || !loopback(window.location.hostname)
      || !loopback(new URL(resolveGateway()).hostname);
    setHostedWeb(hosted);
    if (!local) {
      setLoginMode(hosted ? "account" : "passphrase");
      if (!hosted && !pass) setPass("changeme");
    }
    observedDesktopShell = isDesktopShell();
    setNativeDesktop(observedDesktopShell);
    const installed = () => {
      if (!isDesktopShell()) return;
      observedDesktopShell = true;
      setNativeDesktop(true);
      setSessionAttempt(attempt => attempt + 1);
    };
    window.addEventListener("supymem-desktop-ready", installed);
    return () => window.removeEventListener("supymem-desktop-ready", installed);
  }, []);

  useEffect(() => {
    if (local || !isDesktopShell()) return;
    let disposed = false;
    let stop: (() => void) | undefined;
    const applyStatus = (payload: unknown) => {
      const native = payload as NativeAuthStatus | null;
      const state = native?.state;
      if (!state || disposed) return;
      const previousState = nativeAccountStateRef.current;
      if (native?.restoring === false && nativeAuthActionPending(state) && nativeSessionNeedsSignIn(previousState)) {
        setChecking(false);
        return;
      }
      nativeAccountStateRef.current = state;
      setNativeAccountState(state);
      if (state !== "connected" && native?.subject && !lastConnectedSubjectRef.current) {
        lastConnectedSubjectRef.current = native.subject;
      }
      if (["signed_out", "authorizing", "reauth_required", "account_mismatch", "revocation_pending"].includes(state)) {
        clearVerifiedSession();
        setAuthed(false);
        setSessionSubject(undefined);
        setChecking(false);
        setConnectionError("");
      } else if (state === "connected") {
        setAuthed(true);
        // A browser-approved Mac reconnect can arrive before ensureSession's
        // effect finishes. The usage tracker must receive the newly verified
        // subject; `authed=true` alone silently drops all UI activity.
        const localFounder = native?.subject === "founder"
          && window.location.hostname === "127.0.0.1"
          && new URL(resolveGateway()).hostname === "127.0.0.1";
        if (typeof native?.subject === "string" && (native.subject.startsWith("usr_") || localFounder)) {
          setSessionSubject(native.subject);
        }
        if (previousState !== "connected") setSessionAttempt((n) => n + 1);
        setChecking(false);
        setConnectionError("");
        // A reconnect to the same account keeps the founder's active venture.
        // A verified switch to another account leaves the previous route.
        if (shouldResetPortfolio(previousState, state, lastConnectedSubjectRef.current, native?.subject)) window.location.assign("/");
        if (native?.subject) lastConnectedSubjectRef.current = native.subject;
      }
      else if (nativeAuthAvailabilityMessage(state)) {
        setChecking(false);
        if (native?.restoring) setConnectionError(signInErrorMessage(native.error, nativeAuthAvailabilityMessage(state) || ""));
        // The connection panel owns transient Keychain/operation progress.
        // Replacing the entire login gate here unmounted an active copy-link
        // action and left a stale "Connection interrupted" after it succeeded.
      } else if (state === "offline") {
        setConnectionError("Reconnecting...");
      }
    };
    void desktopListen("supymem-native-auth", applyStatus).then((unsubscribe) => {
      if (disposed) { unsubscribe(); return; }
      stop = unsubscribe;
      void desktopInvoke<NativeAuthStatus>("desktop_auth_status").then(applyStatus);
    });
    return () => { disposed = true; stop?.(); };
  }, [local, nativeDesktop]);

  useEffect(() => listenForUsageSubject(window, ({ subject, recheck }) => {
    // Native requests may clear or change the session independently of the
    // auth-status event. Keep the collector's React prop aligned with the
    // identity event so it cannot silently keep a stale actor indefinitely.
    setSessionSubject(subject);
    if (!subject) setAuthed(false);
    if (recheck && isDesktopShell()) setSessionAttempt((n) => n + 1);
  }, local), [local]);

  const reconnectAttempts = useRef(0);
  useEffect(() => {
    let disposed = false;
    let pending = false;
    let failures = 0;
    const reconnecting = () => {
      if (!local) { setConnectionError("Reconnecting..."); return; }
      if (disposed || pending) return;
      pending = true;
      return ensureSession(undefined, true).catch(() => ({ ok: false })).then(session => {
        if (disposed) return;
        failures = session.ok ? 0 : failures + 1;
        setConnectionError(failures >= 2 ? "Reconnecting..." : "");
      }).finally(() => {
        pending = false;
      });
    };
    window.addEventListener("supymem-session-reconnecting", reconnecting);
    return () => { disposed = true; window.removeEventListener("supymem-session-reconnecting", reconnecting); };
  }, [local]);

  useEffect(() => {
    if (local || connectionError !== "Reconnecting...") { reconnectAttempts.current = 0; return; }
    if (checking) return;
    const timer = window.setTimeout(() => {
      reconnectAttempts.current = Math.min(reconnectAttempts.current + 1, 5);
      setSessionAttempt(attempt => attempt + 1);
    }, Math.min(1000 * 2 ** reconnectAttempts.current, 30000));
    return () => window.clearTimeout(timer);
  }, [local, connectionError, checking, sessionAttempt]);

  useEffect(() => {
    if (!nativeDesktop || !authed || sessionSubject || checking) return;
    // A connected native shell can briefly render before /auth/session returns
    // its actor. If that read failed during a network interruption, retry a
    // read-only session check rather than leaving usage collection off forever.
    const timer = window.setTimeout(() => setSessionAttempt((n) => n + 1), 60000);
    return () => window.clearTimeout(timer);
  }, [nativeDesktop, authed, sessionSubject, checking, sessionAttempt]);

  function resumeMacConnectIfNeeded(): boolean {
    if (local) return false;
    const intent = readDeviceIntent();
    if (!intent) return false;
    return continueDeviceApproval(intent, resolveGateway());
  }

  useEffect(() => {
    if (!authed) return;
    const painted = () => {
      console.info("desktop_startup", { phase: "product_painted", ms: Math.round(performance.now()) });
      if (isDesktopShell()) void desktopInvokeQuiet("desktop_product_painted");
    };
    if (typeof window.requestAnimationFrame !== "function") { const timer = window.setTimeout(painted, 0); return () => window.clearTimeout(timer); }
    const frame = window.requestAnimationFrame(painted);
    return () => window.cancelAnimationFrame(frame);
  }, [authed]);

  async function refreshVentures() {
    if (browserEntry) return true;
    const r = await api("/api/ventures");
    if (r.ok) {
      setAuthed(true);
      setConnectionError("");
      return true;
    }
    setConnectionError("Reconnecting...");
    return false;
  }

  useEffect(() => {
    let cancelled = false;
    if (browserEntry && pathname === "/download") { setChecking(false); return; }
    (async () => {
      setChecking(true);
      try {
        const sess = await ensureSession(undefined, sessionAttempt > 0 || isDesktopShell());
        if (cancelled) return;
        if (sess.ok) {
          setSessionSubject((sess.data as { subject?: string } | null)?.subject);
          setAuthed(true);
          setConnectionError("");
        } else if (!local && isDesktopShell()) {
          const native = await desktopInvoke<NativeAuthStatus>("desktop_auth_status");
          if (cancelled) return;
          if (native?.state) setNativeAccountState(native.state);
          if (isInvalidSession(sess) || nativeSessionNeedsSignIn(native?.state)) {
            if (!nativeSessionNeedsSignIn(native?.state)) setNativeAccountState("reauth_required");
            setAuthed(false);
            setConnectionError("");
          } else {
            setConnectionError(nativeAuthAvailabilityMessage(native?.state) || "Reconnecting...");
          }
        } else if (isInvalidSession(sess) || ((sess.status === 401 || sess.status === 403) && !["core_unavailable", "core_restarting", "upstream_unavailable", "session_unavailable"].includes(String((sess.data as { error?: { code?: string } } | null)?.error?.code)))) {
          setAuthed(false);
          setConnectionError("");
        } else {
          setConnectionError("Reconnecting...");
        }
      } catch {
        if (!cancelled) setConnectionError("Reconnecting...");
      } finally {
        if (!cancelled) setChecking(false);
      }
    })();
    return () => { cancelled = true; };
  }, [local, sessionAttempt, nativeDesktop, browserEntry]);

  async function doLogin(e: FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      if (operatorLogin || (!hostedWeb && loginMode === "passphrase")) {
        const r = await login(pass.trim());
        if (!r.ok) {
          setError(
            r.status === 0 || r.status >= 500
              ? "Could not reach Supymem. Try again shortly."
              : r.status === 429
              ? r.error || "You're going a bit fast - try again in 60 seconds"
              : "That passphrase didn’t work.",
          );
          return;
        }
        setInviteCode(inviteAfterAuthentication(inviteCode, r));
        if (isDesktopShell()) await desktopInvoke("store_passphrase", { passphrase: pass.trim() });
      } else {
        const r = await loginAccount(email.trim(), password, inviteCode);
        if (!r.ok) {
          const code = (r.data as { error?: { code?: string } } | null)?.error?.code;
          if (code === "email_verification_required") {
            setVerificationEmail(email.trim());
            setVerificationMessage(readDeviceIntent()
              ? "Verify your email to continue. After that, the Mac connection still needs approval."
              : "Verify your email to finish setting up your Supymem account.");
            return;
          }
          const invite = inviteErrorMessage(r.data);
          if (invite) { setError(invite); return; }
          setError(r.status === 0 || r.status >= 500
            ? "Could not reach Supymem. Try again shortly."
            : r.status === 429 ? r.error || "You're going a bit fast - try again in 60 seconds" : "Email or password didn’t match.");
          return;
        }
        setInviteCode(inviteAfterAuthentication(inviteCode, r));
      }
      if (resumeMacConnectIfNeeded()) return;
      setAuthed(true);
      await refreshVentures();
    } finally {
      setSubmitting(false);
    }
  }

  async function doCognitoLogin(provider?: "google") {
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await startCognitoLogin(provider, inviteCode);
      const target = (result.data as { authorize_url?: string } | null)?.authorize_url;
      if (!result.ok || !target || !/^https:\/\//.test(target)) {
        setError(result.error || inviteErrorMessage(result.data) || "Supymem could not start secure sign-in. Try again shortly.");
        return;
      }
      window.location.assign(target);
    } finally {
      setSubmitting(false);
    }
  }

  async function doSignup(e: FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const r = await signup(email.trim(), password, inviteCode);
      if (!r.ok) {
        const payload = r.data as { error?: { message?: string; code?: string }; detail?: { message?: string; code?: string } } | null;
        const detail = payload?.error || payload?.detail;
        const invite = inviteErrorMessage(r.data);
        if (invite) { setError(invite); return; }
        setError(r.error || (r.status === 0 || r.status >= 500
          ? accountSignupError(detail?.code) || "Account registration is temporarily unavailable. Please try again shortly."
          : accountSignupError(detail?.code) || "Couldn’t create account. Check the email and password requirements below."));
        return;
      }
      setInviteCode(inviteAfterAuthentication(inviteCode, r));
      const account = r.data as { authenticated?: boolean; email?: string; mode?: string } | null;
      if (account?.authenticated === false && account.mode === "email_verification_required") {
        setAuthed(false);
        setPassword("");
        setVerificationEmail(account.email || email.trim());
        setVerificationMessage(readDeviceIntent()
          ? "We sent a verification link. After you confirm your email, the Mac connection still needs approval. Open the link in this same browser if you can."
          : "We sent a verification link. Open it to activate your account, then sign in here.");
        return;
      }
      if (resumeMacConnectIfNeeded()) return;
      setAuthed(true);
      await refreshVentures();
    } finally {
      setSubmitting(false);
    }
  }

  async function resendVerificationLink() {
    if (!verificationEmail || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const r = await resendVerification(verificationEmail);
      if (r.ok) {
        setVerificationMessage("If that address needs verification, a fresh link is on its way. Check your inbox and spam folder.");
      } else {
        setError(r.error || (
          r.status === 0 || r.status >= 500
            ? "Supymem could not send a verification email right now. Try again shortly."
            : "Please wait a moment before requesting another verification email."
        ));
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function doLogout() {
    const result = await logout();
    if (!result.ok) {
      setConnectionError(signInErrorMessage(result.error, "Could not sign out. Try again."));
      return;
    }
    setInviteCode("");
    const target = (result.data as { upstream_logout_url?: unknown } | null)?.upstream_logout_url;
    if (!nativeDesktop && hostedWeb && typeof target === "string" && /^https:\/\//.test(target)) {
      window.location.assign(target);
      return;
    }
    setAuthed(false);
  }

  // summary.working counts agents configured active, which is not evidence any of
  // them is running. Only claim "working" for agents reporting it live.
  const hired = office?.org?.summary.hired ?? 0;
  const working = (office?.org?.agents || []).filter(
    (agent) => (agent.work?.live_state || "").toLowerCase() === "working",
  ).length;

  const accountPending = nativeAuthActionPending(nativeAccountState);
  const reconnecting = !local && connectionError === "Reconnecting...";
  const connectionMessage = local && connectionError ? "Your app could not be reached. Check that your app is running, then retry." : connectionError;
  if (local && !authed) {
    return (
      <main aria-live="polite" data-webview-pending={checking ? "workspace_session" : undefined}>
        <p role={checking ? "status" : "alert"}>{checking ? "Opening your workspace. Please wait." : "Your app could not be reached. Check that your app is running, then retry."}</p>
        {!checking && <button type="button" onClick={reconnectInPlace}>Retry</button>}
      </main>
    );
  }
  if (browserEntry && pathname === "/download") return <main className="login-gate"><section className="login-card">{children}</section></main>;
  if (!authed && nativeDesktop && !nativeSessionNeedsSignIn(nativeAccountState)) {
    return (
      <main className="login-gate">
        <UsageTracker subject={sessionSubject} pathname={pathname} ventureId={effectiveVentureId} />
        <div className="login-card" role="status" aria-live="polite">
          <p className="login-kicker">Supymem</p>
          <h1>Restoring your session…</h1>
          <p>{nativeAccountState === "secure_store_pending" ? "Approve the Keychain prompt to continue" : connectionError || "Checking your saved session with Supymem…"}</p>
          {!checking && !accountPending && <button data-dead-end="reconnect_shown" className="primary" onClick={reconnectInPlace}>Reconnect</button>}
        </div>
      </main>
    );
  }
  if (!authed && (checking || connectionError)) {
    return (
      <div className="login-gate">
        <UsageTracker subject={sessionSubject} pathname={pathname} ventureId={effectiveVentureId} />
        <div className="login-card" role="status" aria-live="polite">
          <p className="login-kicker">Supymem</p>
          <h1>{reconnecting ? "Reconnecting..." : nativeAccountState === "secure_store_pending" ? "Waiting for macOS approval" : nativeAccountState === "native_auth_busy" ? "Finishing account request" : checking ? "Signing you in…" : "Connection interrupted"}</h1>
          <p>{checking ? "Checking your sign-in…" : connectionError}</p>
          {!checking && !accountPending && <button data-dead-end="reconnect_shown" className="primary" onClick={reconnectInPlace}>Reconnect</button>}
          {!checking && nativeDesktop && <DesktopAccountConnection onConnected={() => setSessionAttempt((n) => n + 1)} />}
        </div>
      </div>
    );
  }

  if (!authed && verificationEmail) {
    return (
      <div className="login-gate">
        <div className="login-card" aria-live="polite">
          <p className="login-kicker">Supymem</p>
          <h1>Verify your email</h1>
          <p>{verificationMessage || "Open the verification link we sent to activate your account."}</p>
          <p className="muted">{verificationEmail}</p>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <button className="primary" type="button" disabled={submitting} onClick={() => void resendVerificationLink()}>
              {submitting ? "Sending…" : "Resend verification email"}
            </button>
            <button
              className="ghost"
              type="button"
              disabled={submitting}
              onClick={() => {
                setVerificationEmail("");
                setVerificationMessage("");
                setError("");
              }}
            >
              Back to sign in
            </button>
          </div>
          <ErrorNotice error={error} source="AppShell.error" as="div" className="error" />
        </div>
      </div>
    );
  }

  if (!authed) {
    if (nativeDesktop) {
      return (
        <main className="login-gate">
          <div className="login-card">
            <p className="login-kicker">Supymem</p>
            <h1>Sign in to Supymem</h1>
            <p>Sign in with your browser to continue.</p>
            <DesktopAccountConnection onConnected={() => setSessionAttempt((n) => n + 1)} />
          </div>
        </main>
      );
    }
    return (
      <div className="login-gate">
        <div className="login-card">
          <p className="login-kicker">Supymem</p>
          <h1>{hostedWeb ? "Sign in to Supymem" : "A team of AI agents for your venture"}</h1>
          <p>
            {hostedWeb
              ? "Sign in with Google or email to continue."
              : "They share one brain, claim real work, and stay visible. Everything runs on this machine."}
          </p>
          {!hostedWeb && <div className="login-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={loginMode === "passphrase"}
              className={cx("ghost sm", loginMode === "passphrase" && "active")}
              onClick={() => {
                setLoginMode("passphrase");
                setError("");
              }}
            >
              Passphrase
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={loginMode === "account"}
              className={cx("ghost sm", loginMode === "account" && "active")}
              onClick={() => {
                setLoginMode("account");
                setError("");
              }}
            >
              Local account
            </button>
          </div>}
          {operatorLogin || (!hostedWeb && loginMode === "passphrase") ? (
            <form onSubmit={(e) => void doLogin(e)}>
              <input
                type="password"
                value={pass}
                onChange={(e) => setPass(e.target.value)}
                placeholder="Console passphrase"
                autoFocus
              />
              <button className="primary" type="submit" data-usage-control="account_operator_sign_in" disabled={submitting}>
                {submitting ? "Signing in…" : "Sign in"}
              </button>
              <ErrorNotice error={error} source="AppShell.error" as="div" className="error" />
              <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
                {hostedWeb
                  ? "Use your console passphrase."
                  : "Default for local dogfood: changeme"}
              </p>
            </form>
          ) : hostedWeb ? (
            <div style={{ display: "grid", gap: "0.75rem" }}>
              <button className="google-sign-in" type="button" data-usage-control="account_sign_in_google" disabled={submitting} onClick={() => void doCognitoLogin("google")}>
                {/* Official Google HTML button configurator mark. */}
                <svg aria-hidden="true" width="20" height="20" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
                  <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
                  <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
                  <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
                  <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
                </svg>
                Sign in with Google
              </button>
              <button className="ghost" type="button" data-usage-control="account_sign_in_email" disabled={submitting} onClick={() => void doCognitoLogin()}>
                Sign in with email
              </button>
              <ErrorNotice error={error} source="AppShell.error" as="div" className="error" />
            </div>
          ) : (
            <form onSubmit={(e) => void doLogin(e)}>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoFocus
                autoComplete="username"
              />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Password (12+ characters)"
                autoComplete="current-password"
              />
              <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>{PASSWORD_REQUIREMENTS}</p>
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <button className="primary" type="submit" disabled={submitting}>
                  {submitting ? "Please wait…" : "Sign in"}
                </button>
                <button className="ghost" type="button" disabled={submitting} onClick={(e) => void doSignup(e)}>
                  Create account
                </button>
              </div>
              <ErrorNotice error={error} source="AppShell.error" as="div" className="error" />
              <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
                {hostedWeb ? "Your account and company state are stored securely in Supymem’s hosted service." : "This local development account is stored on this Mac."}
              </p>
            </form>
          )}
          {!operatorLogin && (hostedWeb || loginMode === "account") && <PasswordResetForm />}
        </div>
      </div>
    );
  }

  if (browserEntry) return <main className="login-gate"><section className="login-card">{children}</section></main>;

  if (!founderUILoaded && chatFirstHidden(pathname || "")) return <div className="page-loading" role="status" data-webview-pending="route_load">Loading your company workspace...</div>;
  if (hiddenPage) return null;  // redirecting to the chat; never flash or run a hidden page
  if (founderUI === "home") return (
    <FounderUIContext.Provider value="home">
      <div ref={local ? localHomeRef : undefined} data-venture-id={effectiveVentureId} onClick={captureChatLink} className={cx("app-shell", "native-shell", "founder-shell", "home-shell", local && pathname.startsWith("/settings/local") && "local-settings-shell", (effectiveVentureId || entryPage) && "venture-frame", (bare || pathname === "/") && "app-shell-bare", chatFirstHome && "chat-first-home", chatShown && "chat-open", chatShown && !chatFirstHome && !officeTab && "chat-page-layout")}
        onKeyDown={local ? event => {
          if (event.defaultPrevented || event.key !== "Escape" || !(event.target instanceof Element)) return;
          const details = event.target.closest<HTMLDetailsElement>("details[open]");
          if (!details) return;
          event.preventDefault();
          event.stopPropagation();
          details.open = false;
          details.querySelector("summary")?.focus();
        } : undefined}
        style={{ "--chat-panel-width": `${chatPanelSize.width}px` } as CSSProperties}>
        <UsageTracker subject={authed ? sessionSubject : undefined} pathname={pathname} ventureId={effectiveVentureId} />
        <AppShellErrorBoundary>
          <ChatOpenContext.Provider value={chatShown}>
            <ClarificationSkipContext.Provider value={clarificationSkip || null}>
              {effectiveVentureId && chatAvailable && !chatShown && !localBuildActivity && !localSubpage && pathname !== "/connections" && <button ref={chatToggleRef} type="button" className="charlie-buddy" onClick={() => setChatPanelOpen(true)} aria-label={`Open chat with ${managerName}`} aria-expanded={false} aria-controls="supymem-chat-panel">{local ? <CharlieMascot /> : <CharlieFace />}</button>}
              {!(bare || pathname === "/") && <div className="shell-titlebar" data-tauri-drag-region onMouseDown={event => { if (nativeDesktop && event.button === 0 && event.target === event.currentTarget) void desktopInvokeQuiet("plugin:window|start_dragging"); }}>
                <div className="shell-titlebar-leading" aria-hidden="true"><div className="shell-titlebar-traffic" /><div className="shell-titlebar-slot" /></div>
                <div className="shell-titlebar-main" data-tauri-drag-region onMouseDown={event => { if (nativeDesktop && event.button === 0 && event.target === event.currentTarget) void desktopInvokeQuiet("plugin:window|start_dragging"); }} />
                <div className="shell-titlebar-trailing" aria-hidden="true"><div className="shell-titlebar-slot" /></div>
              </div>}
              {!local && !effectiveVentureId && !entryPage && !(bare || pathname === "/") && <aside className="sidebar">
                <div className="sidebar-brand"><WorkspaceSwitcher currentId={effectiveVentureId} onRenamed={setSavedTitle} /></div>
                <div className="sidebar-main"><Link className="sidebar-company" href="/"><NAV_ICONS.ventures />Companies</Link><GuideNavLink pathname={pathname || ""} /></div>
                <div className="sidebar-spacer" />
                {!local && settingsOpen && <div className="sidebar-foot"><div className="muted">Mac account</div>{nativeDesktop && <DesktopAccountConnection compact expectedSubject={sessionSubject} onConnected={() => setSessionAttempt(attempt => attempt + 1)} />}<button type="button" className="ghost sm" onClick={() => void doLogout()}>Log out</button></div>}
                <button className="ghost sm sidebar-gear sidebar-settings" type="button" onClick={() => local ? router.push("/settings/local") : setSettingsOpen(true)} aria-label="Settings" aria-expanded={settingsOpen}><span aria-hidden="true">⚙</span> Settings</button>
              </aside>}
              <section className={cx("workspace", localBuildActivity && "build-activity-workspace", localSubpage && "local-subpage-workspace")} role={localBuildActivity ? "main" : undefined}
                tabIndex={localBuildActivity ? -1 : undefined} aria-label={localBuildActivity ? shownTitle : undefined}>
                {!localBuildActivity && (effectiveVentureId || entryPage || local && pathname !== "/") ? <CompanyOffice ventureId={effectiveVentureId || ""} pathname={pathname} standalone={localSubpage} onSettings={() => setSettingsOpen(true)} chatAvailable={chatAvailable || entryPage} title={shownTitle} scrollRef={workspaceBodyRef} nextStep={(!local || isVentureOverview || entryPage) && <NextStepStrip ventureId={effectiveVentureId} ideaStatus={ideaStatus} inline />} controls={<>
                  {!isVentureOverview && showsDetailToggle(pathname) && <DetailToggle />}
                  {!isVentureOverview && headerRight && <details className="home-page-controls"><summary>Page controls</summary>{headerRight}</details>}
                </>} notices={<>
                  {local && searchParams.get("local_notice") === "1" && <p role="status">Open your venture to continue your work, or <a href="/settings/local">open Settings &gt; Keys and connections</a> to manage your local app.</p>}
                  <AttentionToast ventureId={effectiveVentureId} />
                  {(!chatShown || officeTab) && <DesktopNoticeBanner ventureId={effectiveVentureId} otherVentureTarget="company-office-notice" />}
                  {connectionError && <div className={reconnecting ? "muted" : "error"} data-dead-end={!accountPending && !reconnecting ? "reconnect_shown" : undefined} role={accountPending || reconnecting ? "status" : "alert"}>{connectionMessage} {!accountPending && !reconnecting && <button className="ghost sm" type="button" disabled={checking} onClick={reconnectInPlace}>{checking ? "Connecting…" : local ? "Retry" : "Reconnect"}</button>}</div>}
                  {!local && permissionRequest && <DesktopTaskPermissionCard key={permissionRequestKey} onGranted={onMacTaskPermissionsGranted} />}
                </>}>
                  {!entryPage && <WorkspaceErrorBoundary>{local && pathname === "/connections" && <LocalConnections />}{companyOperations ? <OpsWorkspaceGate onScopeChange={setOperationsScope} hideVentureControls={pathname === "/gtm" || pathname === "/connections"}>{children}</OpsWorkspaceGate> : children}</WorkspaceErrorBoundary>}
                  {bottomBar != null && <div className="workspace-bottom">{bottomBar}</div>}
                </CompanyOffice> : <>
                  {!(bare || pathname === "/") && <div className="workspace-fixed-header"><header className="workspace-header"><div className="page-heading">{localBuildActivity && effectiveVentureId ? <Link href={`/v/${encodeURIComponent(effectiveVentureId)}`} data-chat-route-native={true}>Back to chat</Link> : <Link href="/">Back to company</Link>}{!guidePage && <h1>{shownTitle || "Supymem"}</h1>}{subtitle && <p>{subtitle}</p>}</div></header>{localBuildActivity && <AttentionToast ventureId={effectiveVentureId} />}<DesktopNoticeBanner ventureId={effectiveVentureId} />{connectionError && (reconnecting ? <div className="muted" role="status">Reconnecting...</div> : <div data-dead-end="reconnect_shown"><ErrorNotice className="error" as="div" error={connectionMessage} source="shell.connection">{connectionMessage} <button type="button" onClick={reconnectInPlace}>{local ? "Retry" : "Reconnect"}</button></ErrorNotice></div>)}</div>}
                  <div ref={workspaceBodyRef} className={cx("workspace-body", fill && "fill")}>{!local && permissionRequest && <DesktopTaskPermissionCard key={permissionRequestKey} onGranted={onMacTaskPermissionsGranted} />}<WorkspaceErrorBoundary>{children}</WorkspaceErrorBoundary></div>
                </>}
              </section>
              {effectiveVentureId && !entryPage && <VentureChatLauncher key={effectiveVentureId} open={chatShown && !officeTab} primary={chatFirstHome} ventureId={effectiveVentureId} subject={sessionSubject} size={chatPanelSize} onSizeChange={setChatPanelSize} closable={false} onMinimize={() => { setChatPanelOpen(false); chatToggleRef.current?.focus(); }} pendingAsk={pendingAsk} onAskSent={() => setPendingAsk(null)} />}
              {entryPage && <main id="supymem-chat-panel" className={`${chatPanelStyles.panel} ${chatPanelStyles.homePanel}`} tabIndex={-1} aria-labelledby="entry-chat-title" data-webview-main="entry">
                <h1 id="entry-chat-title" className={chatDockStyles.replyAnnouncement}>{shownTitle || "Company conversation"}</h1>
                <WorkspaceErrorBoundary>{children}</WorkspaceErrorBoundary>
              </main>}
              {!local && !effectiveVentureId && !entryPage && !(bare || pathname === "/") && <CharlieDock local={local} managerName={managerName} open={chatPanelOpen} chatAvailable={false} onOpen={() => setChatPanelOpen(true)} onClose={() => setChatPanelOpen(false)} />}
              {!local && settingsOpen && <CompanySettingsDialog key={`${effectiveVentureId || "account"}:${settingsSection}`} ventureId={effectiveVentureId} initialSection={settingsSection} onClose={() => setSettingsOpen(false)} />}
              <AgentDesk />
            </ClarificationSkipContext.Provider>
          </ChatOpenContext.Provider>
        </AppShellErrorBoundary>
      </div>
    </FounderUIContext.Provider>
  );



  return (
    <div
      className={cx("app-shell", "native-shell", chatPanelOpen && chatAvailable && "chat-open", sidebarPinned && "sidebar-pinned", sidebarExpanded && "sidebar-expanded")}
      style={{ "--chat-panel-width": `${chatPanelSize.width}px` } as CSSProperties}
    >
      <UsageTracker subject={authed ? sessionSubject : undefined} pathname={pathname} ventureId={effectiveVentureId} />
      <AppShellErrorBoundary>
      <ChatOpenContext.Provider value={chatAvailable && chatPanelOpen}>
      <ClarificationSkipContext.Provider value={clarificationSkip || null}>
      <aside className={cx("sidebar", sidebarPinned && "pinned", sidebarExpanded && "expanded")}
        onMouseDownCapture={event => {
          if (event.button !== 0) return;
          const link = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
          if (!link) return;
          if (sidebarExpanded || sidebarPinned) link.focus({ preventScroll: true });
          else {
            closeSidebar();
            event.preventDefault();
          }
        }}
        onPointerEnter={event => {
          if (event.pointerType === "touch" || sidebarPinned) return;
          const rail = event.currentTarget;
          closeSidebar();
          sidebarHover.current = setTimeout(() => {
            sidebarHover.current = null;
            const input = document.activeElement?.closest("input, textarea, select, [contenteditable]:not([contenteditable=false])");
            if (input && !rail.contains(input)) return;
            setSidebarExpanded(true);
          }, 250);
        }} onPointerLeave={closeSidebar}>
        <div className="sidebar-brand">
          <WorkspaceSwitcher
            currentId={effectiveVentureId}
            key={effectiveVentureId || "all"}
            onRenamed={setSavedTitle}
            onResolved={setVentureName}
          >
            {effectiveVentureId ? <Link
              href={local ? "/settings/local" : `/v/${encodeURIComponent(effectiveVentureId)}/settings`}
              className="ghost sidebar-gear"
              aria-label="Venture settings"
              title="Venture settings"
            >
              <NAV_ICONS.settings />
            </Link> : <button
              className="ghost sidebar-gear"
              type="button"
              onClick={() => setSettingsOpen((v) => !v)}
              aria-label={local ? "Your settings" : nativeDesktop ? "Mac account connection" : "Account settings"}
              aria-expanded={settingsOpen}
              title={local ? "Your settings" : nativeDesktop ? "Mac account connection" : "Account settings"}
            >
              <NAV_ICONS.settings />
            </button>}
          </WorkspaceSwitcher>
        </div>

        <div className="sidebar-main">
          <SideNav pathname={pathname} ventureId={effectiveVentureId} operationsScope={activeScope} />
          {ventureId && pathname !== "/new" && <div className="sidebar-team"><OfficeTeamRail /></div>}
        </div>

        <div className="sidebar-spacer" />
        {!local && settingsOpen && (
          <div className="sidebar-foot">
            <div className="muted" style={{ fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.06em" }}>
              {nativeDesktop ? "Mac account" : "Account"}
            </div>
            <a href="/account" data-usage-control="account_settings_open">Account settings</a>
            {effectiveVentureId && (
              <a href={`/v/${encodeURIComponent(effectiveVentureId)}/settings`}>
                Venture settings (Computer Use)
              </a>
            )}
            <a href="/">Company overview</a>
            <ReportProblem ventureId={effectiveVentureId} />
            {!local && nativeDesktop && <DesktopAccountConnection compact expectedSubject={sessionSubject} onConnected={() => setSessionAttempt((n) => n + 1)} />}
            <button
              type="button"
              className="ghost sm"
              onClick={() => void doLogout()}
            >
              Log out
            </button>
          </div>
        )}
        <button
          className="ghost sidebar-gear sidebar-pin"
          type="button"
          onClick={togglePin}
          aria-pressed={sidebarPinned}
          aria-label={sidebarPinned ? "Unpin sidebar" : "Pin sidebar open"}
          title={sidebarPinned ? "Unpin sidebar" : "Pin sidebar open"}
        >
          <NAV_ICONS.sidebar />
        </button>
      </aside>

      <div className="shell-status"><RunStatusBar ventureId={effectiveVentureId} ideaStatus={ideaStatus} />{effectiveVentureId && !isVentureOverview && (!ideaStatus || clarificationSkip) && <NextStepStrip ventureId={effectiveVentureId} ideaStatus={ideaStatus} />}</div>

      <section className="workspace">
        <div className="workspace-fixed-header">
        <header className="workspace-header">
          <div className="page-heading">
            {!guidePage && <h1>{shownTitle || "Supymem"}</h1>}
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          {(shownStatus?.label || stage) && (
            <span
              className={cx(
                "stage-pill",
                (shownStatus?.code === "paused" || /paused/i.test(String(shownStatus?.label || stage))) &&
                  "stage-pill-paused",
                (shownStatus?.code === "live" || shownStatus?.code === "watching") && "stage-pill-live",
                shownStatus?.code === "needs_you" && "stage-pill-needs",
              )}
              title={shownStatus?.detail || undefined}
            >
              {ventureStatusLabel(stage, shownStatus)}
            </span>
          )}
          {effectiveVentureId && hired > 0 && (
            <span className="stage-pill">
              {teamCountLabel(hired, working)}
            </span>
          )}
          <div className="spacer" />
          {effectiveVentureId && showsDetailToggle(pathname) && <DetailToggle />}
          {headerRight}
          {chatAvailable && (
            <button ref={chatToggleRef} type="button" className="ghost sm" onClick={() => setChatPanelOpen((open) => !open)} aria-expanded={chatPanelOpen} aria-controls="supymem-chat-panel">
              {chatPanelOpen ? "Hide chat" : `Ask ${managerName}`}
            </button>
          )}
        </header>
        <AttentionToast ventureId={effectiveVentureId} />
        <DesktopNoticeBanner ventureId={effectiveVentureId} />
        {connectionError && (
          <div className={reconnecting ? "muted" : "error"} data-dead-end={!accountPending && !reconnecting ? "reconnect_shown" : undefined} role={accountPending || reconnecting ? "status" : "alert"} style={{ margin: "0 24px 12px" }}>
            {connectionMessage}{" "}
            {!accountPending && !reconnecting && <button className="ghost sm" type="button" disabled={checking} onClick={reconnectInPlace}>
              {checking ? "Connecting…" : local ? "Retry" : "Reconnect"}
            </button>}
          </div>
        )}
        </div>
        <div ref={workspaceBodyRef} className={cx("workspace-body", fill && "fill")}>
          {!local && permissionRequest && <DesktopTaskPermissionCard key={permissionRequestKey} onGranted={onMacTaskPermissionsGranted} />}
          <WorkspaceErrorBoundary>
            {pathname !== "/gtm" && <FounderCoaching ventureId={effectiveVentureId} />}
            {local && pathname === "/connections" && <LocalConnections />}{companyOperations ? <OpsWorkspaceGate onScopeChange={setOperationsScope} hideVentureControls={pathname === "/gtm" || pathname === "/connections"}>{children}</OpsWorkspaceGate> : children}
          </WorkspaceErrorBoundary>
        </div>
        {bottomBar != null ? <div className="workspace-bottom">{bottomBar}</div> : null}
      </section>

      {chatAvailable && effectiveVentureId ? (
        <div className="chat-panel" hidden={!chatPanelOpen} inert={!chatPanelOpen}>
        <VentureChatLauncher
          key={effectiveVentureId}
          open={chatPanelOpen}
          ventureId={effectiveVentureId}
          subject={sessionSubject}
          size={chatPanelSize}
          onSizeChange={setChatPanelSize}
          onMinimize={() => { setChatPanelOpen(false); chatToggleRef.current?.focus(); }}
          pendingAsk={pendingAsk}
          onAskSent={() => setPendingAsk(null)}
        />
        </div>
      ) : null}
      <AgentDesk />
      </ClarificationSkipContext.Provider>
      </ChatOpenContext.Provider>
      </AppShellErrorBoundary>
    </div>
  );
}

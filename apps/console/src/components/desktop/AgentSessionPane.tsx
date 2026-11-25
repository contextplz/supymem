"use client";

import { routeInChat } from "@/lib/chatRouteAction";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  desktopInvoke,
  desktopInvokeOrThrow,
  desktopListen,
  desktopListenOrThrow,
  invokeErrorMessage,
  isDesktopShell,
  isUnknownDesktopCommand,
} from "@/lib/desktop";
import { parseControlChunk, type TranscriptItem } from "@/lib/codingControl";
import { safeLoopText } from "@/lib/loopProgress";
import { AcpSessionClient } from "@/lib/acpSessionClient";
import type { AcpEvent, RpcId } from "@/lib/acpSessionProtocol";
import { COMPUTER_USE } from "@/lib/computerUse";
import { announceUsageControl } from "@/lib/usageControl";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

import { MAC_TASK_PERMISSION_EVENT, macTaskPermissionsGranted, type PermissionSnapshot } from "@/lib/macPermissions";
import { DesktopSurfacePicker } from "./DesktopSurfacePicker";
import { BrowserProfilePreparation } from "./BrowserProfilePreparation";
import { reviewedSurfaceBody, type SurfaceReviewReceipt } from "@/lib/desktopSurfaceReview";
import { api, verifiedSessionEpoch } from "@/lib/client";
import { desktopExecutionAvailability, desktopExecutionMessage, type DesktopExecutionAvailability } from "@/lib/desktopExecutionAvailability";
import { isLocalConsole } from "@/lib/localMode";
import { apiErrorCode, apiFailure } from "@/lib/apiFailure";

const TOOLS = [
  { name: "cursor", label: "Cursor", bin: "cursor-agent", protocol: "acp" as const, auth: null },
  { name: "claude", label: "Claude Code", bin: "claude", protocol: "print" as const, auth: null },
  { name: "codex", label: "Codex", bin: "codex", protocol: "print" as const, auth: null },
  { name: "opencode", label: "OpenCode", bin: "opencode", protocol: "acp" as const, auth: null },
  { name: "hermes", label: COMPUTER_USE, bin: "hermes", protocol: "acp" as const, auth: null },
];

type LocalCli = { name: string; bin: string; path: string };

export function AgentSessionPane({
  ventureId,
  folderName,
  seedPrompt,
  provider: forcedProvider,
  autoStart,
  onFinished,
  onProgress,
  onFailed,
  onPreview,
}: {
  ventureId: string;
  folderName?: string;
  seedPrompt?: string;
  provider?: string;
  autoStart?: boolean;
  autoApprove?: boolean;
  onFinished?: () => void;
  onProgress?: (text: string) => void;
  onFailed?: (message: string) => void;
  onPreview?: (url: string) => void;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  const ownerEpoch = verifiedSessionEpoch();
  const preparationOwner = useRef({ventureId, epoch: ownerEpoch, generation: 0});
  if (preparationOwner.current.ventureId !== ventureId || preparationOwner.current.epoch !== ownerEpoch) {
    preparationOwner.current = {ventureId, epoch: ownerEpoch, generation: preparationOwner.current.generation + 1};
  }
  const renderedPreparationOwner = preparationOwner.current;
  const clientRef = useRef<AcpSessionClient | null>(null);
  const turnRef = useRef(0);
  const sendingRef = useRef(false);
  const acpSession = useRef<string | null>(null);
  const controlIdRef = useRef<string | null>(null);
  const providerRef = useRef(forcedProvider || "cursor");
  const finishedRef = useRef(false);
  const startedRef = useRef(false);
  const startRef = useRef<(prompt: string) => Promise<void>>(async () => {});
  const completeLandingRef = useRef<(reason: string) => Promise<void>>(async () => {});
  const onFinishedRef = useRef(onFinished);
  const onProgressRef = useRef(onProgress);
  const onFailedRef = useRef(onFailed);
  const onPreviewRef = useRef(onPreview);
  onFinishedRef.current = onFinished;
  onProgressRef.current = onProgress ? (text: string) => onProgress(safeLoopText(text, 2000)) : undefined;
  onFailedRef.current = onFailed;
  onPreviewRef.current = onPreview;
  const [surfaceReview, setSurfaceReview] = useState<SurfaceReviewReceipt | null>(null);
  const [nativeAvailabilityReceipt, setNativeAvailabilityReceipt] = useState<{ owner: typeof renderedPreparationOwner; status: DesktopExecutionAvailability } | null>(null);
  const nativeAvailability = nativeAvailabilityReceipt?.owner === renderedPreparationOwner ? nativeAvailabilityReceipt.status : "unknown";
  const [desktop, setDesktop] = useState(false);
  const [clis, setClis] = useState<LocalCli[]>([]);
  const [provider, setProvider] = useState(forcedProvider || "cursor");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [resume] = useState<string | null>(null);
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [input, setInput] = useState(seedPrompt || "");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<"idle" | "running" | "needs_app">("idle");
  const [permissions, setPermissions] = useState<Extract<AcpEvent, { kind: "permission" }>[]>([]);
  const [answeringPermission, setAnsweringPermission] = useState(false);
  const [oldApp, setOldApp] = useState(false);
  const [ideNote, setIdeNote] = useState("");
  const [cliSession, setCliSession] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");

  useEffect(() => {
    setDesktop(isDesktopShell());
  }, []);

  useEffect(() => {
    providerRef.current = forcedProvider || provider;
  }, [forcedProvider, provider]);

  useEffect(() => {
    if ((forcedProvider || provider) !== "hermes") return;
    let active = true, serial = 0;
    const refresh = () => {
      const generation = ++serial;
      const owner = preparationOwner.current;
      setNativeAvailabilityReceipt(null); setSurfaceReview(null);
      void api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`).then(result => {
        if (active && generation === serial && owner === preparationOwner.current && owner.epoch === verifiedSessionEpoch()) {
          setNativeAvailabilityReceipt({ owner, status: desktopExecutionAvailability(result) });
        }
      });
    };
    refresh();
    window.addEventListener("supymem-session-changed", refresh);
    window.addEventListener("supymem-desktop-pause-changed", refresh);
    return () => { active = false; serial++; window.removeEventListener("supymem-session-changed", refresh); window.removeEventListener("supymem-desktop-pause-changed", refresh); };
  }, [ventureId, forcedProvider, provider, renderedPreparationOwner]);

  useEffect(() => {
    if (!desktop) return;
    if ((forcedProvider || provider) === "cursor") {
      setOldApp(false);
      return;
    }
    let cancelled = false;
    async function probe(attempt = 0) {
      const found = await desktopInvoke<LocalCli[]>("list_local_clis");
      if (cancelled) return;
      if (Array.isArray(found)) {
        setClis(found);
        setOldApp(false);
        if (!forcedProvider) {
          const first = TOOLS.find((t) => found.some((c) => c.name === t.name));
          if (first) setProvider(first.name);
        }
        return;
      }
      if (attempt < 2) {
        window.setTimeout(() => void probe(attempt + 1), 350);
        return;
      }
      setOldApp(true);
    }
    void probe();
    return () => {
      cancelled = true;
    };
  }, [desktop, forcedProvider, provider]);

  useEffect(() => {
    if (!desktop || isLocalConsole()) return;
    let unsubIde: (() => void) | undefined;
    void desktopListen("ide-progress", (payload) => {
      const rec = payload as { text?: string; files?: string[]; done?: boolean; preview_url?: string };
      const line = rec?.text;
      if (rec?.preview_url) {
        setPreviewUrl(rec.preview_url);
        onPreviewRef.current?.(rec.preview_url);
      }
      if (rec?.done) {
        void completeLandingRef.current(line || "Preview is live. Review it, then click Landing page looks ready.");
        return;
      }
      if (line) {
        setItems((prev) => [...prev, { kind: "status", text: line }]);
        onProgressRef.current?.(line.slice(0, 72));
        if (/wrote/i.test(line)) setErr("");
      }
    }).then((unsub) => {
      unsubIde = unsub;
    });
    return () => {
      unsubIde?.();
    };
  }, [desktop]);

  useEffect(() => {
    if (!desktop) return;
    let cancelled = false;
    const subscriptions: (() => void)[] = [];
    acpSession.current = null;
    controlIdRef.current = null;
    startedRef.current = false;
    finishedRef.current = false;
    sendingRef.current = false;
    setSessionId(null); setCliSession(null); setPermissions([]); setItems([]);
    setPhase("idle"); setBusy(false); setErr(""); setPreviewUrl("");
    async function attach(name: string, callback: (payload: unknown) => void) {
      const unsub = await desktopListen(name, callback);
      if (cancelled) unsub();
      else subscriptions.push(unsub);
    }
    void (async () => {
      await attach("stdio-output", payload => {
        const rec = payload as { id?: string; data?: string };
        const controlId = controlIdRef.current;
        if (!controlId || rec?.id !== controlId || clientRef.current || !rec.data) return;
        const parsed = parseControlChunk(providerRef.current, rec.data);
        const visible = parsed.items.filter(item => item.kind !== "thought");
        if (visible.length) setItems(prev => mergeItems(prev, visible));
      });
      if (cancelled) return;
      await attach("stdio-exit", payload => {
        const rec = payload as { id?: string };
        if (!controlIdRef.current || rec?.id !== controlIdRef.current || clientRef.current) return;
        controlIdRef.current = null;
        setSessionId(null);
        setPhase("idle");
        setBusy(false);
        setItems(prev => [...prev, { kind: "status", text: "The local process exited. Review its output; exit alone does not verify the result." }]);
      });
    })();
    return () => {
      cancelled = true;
      turnRef.current++;
      subscriptions.forEach(unsub => unsub());
      const client = clientRef.current;
      clientRef.current = null;
      if (client) void client.stop().catch(() => onFailedRef.current?.("The Mac app could not confirm stopping the local session."));
    };
  }, [desktop, ventureId]);

  useEffect(() => {
    logRef.current?.scrollTo(0, logRef.current.scrollHeight);
  }, [items]);

  async function completeLanding(reason: string) {
    if (finishedRef.current) return;
    finishedRef.current = true;
    setPhase("idle");
    setBusy(false);
    setErr("");
    setItems((prev) => [...prev, { kind: "status", text: reason }]);
    onProgressRef.current?.(reason.slice(0, 72));
  }
  completeLandingRef.current = completeLanding;

  useEffect(() => {
    if (!desktop || isLocalConsole()) return;
    const name = forcedProvider || provider;
    if (name !== "cursor" && name !== "claude" && name !== "codex") return;
    const t = window.setInterval(() => {
      void desktopInvoke<{
        ready?: boolean;
        html?: string;
        preview_url?: string;
      }>("read_venture_landing", {
        ventureId,
        folderName: folderName || undefined,
      }).then((snap) => {
        if (snap?.preview_url) {
          setPreviewUrl(snap.preview_url);
          onPreviewRef.current?.(snap.preview_url);
        }
        if (!finishedRef.current && snap?.ready) {
          void completeLandingRef.current(
            snap.preview_url
              ? `Preview is live at ${snap.preview_url}, review it, then click Landing page looks ready.`
              : "Landing page files are ready. Review them, then click Landing page looks ready.",
          );
        }
      });
    }, 4000);
    return () => window.clearInterval(t);
  }, [desktop, forcedProvider, provider, ventureId, folderName]);

  useEffect(() => {
    if (!autoStart || !seedPrompt || startedRef.current || !desktop) return;
    let cancelled = false;
    let startTimer: ReturnType<typeof setTimeout> | undefined;
    startedRef.current = true;
    if (isLocalConsole()) {
      startTimer = setTimeout(() => {if (!cancelled) void startRef.current(seedPrompt);}, 500);
      return () => {cancelled = true; if (startTimer) clearTimeout(startTimer);};
    }
    void desktopInvoke<{ ready?: boolean; html?: string; preview_url?: string }>("read_venture_landing", {
      ventureId,
      folderName: folderName || undefined,
    }).then((snap) => {
      if (cancelled) return;
      if (snap?.preview_url) {
        setPreviewUrl(snap.preview_url);
        onPreviewRef.current?.(snap.preview_url);
      }
      if (snap?.ready && (snap.html || "").length > 40) {
        void completeLandingRef.current(
          snap.preview_url
            ? `Preview is live at ${snap.preview_url}, review it, then click Landing page looks ready.`
            : "Landing page files are ready. Review them, then click Landing page looks ready.",
        );
        return;
      }
      startTimer = setTimeout(() => { if (!cancelled) void startRef.current(seedPrompt); }, 500);
    });
    return () => { cancelled = true; if (startTimer) clearTimeout(startTimer); };
  }, [autoStart, seedPrompt, desktop, ventureId, folderName]);

  const known = TOOLS.find((t) => t.name === provider);
  const isComputerUse = provider === "hermes";
  const toolLabel = known?.label || (isComputerUse ? COMPUTER_USE : provider);
  const cliReady = clis.some((c) => c.name === provider);

  if (!desktop) {
    return (
      <section className="panel-card" style={{ marginTop: "1rem" }}>
        <h3 style={{ margin: 0 }}>{toolLabel} on this Mac</h3>
        <p className="muted" style={{ marginTop: 8 }}>
          Open the Supymem Mac app to run {toolLabel} on this computer. A browser tab cannot start
          local tools.
        </p>
      </section>
    );
  }


  async function startOrSend(prompt: string) {
    const capturedOwner = {...renderedPreparationOwner};
    if (renderedPreparationOwner !== preparationOwner.current || capturedOwner.epoch !== verifiedSessionEpoch()) return;
    if (sendingRef.current) return;
    setErr("");
    const text = prompt.trim();
    if (!text) return;
    if (isComputerUse) announceUsageControl("computer_use_send");
    sendingRef.current = true;
    const turn = ++turnRef.current;
    setBusy(true);
    setItems((prev) => [...prev, { kind: "you", text }]);
    const proto = known?.protocol || "acp";
    let failureMessage = "Your build request could not be confirmed. Open Builds to check its saved status before starting another build.";

    try {
      if (!isComputerUse && isLocalConsole()) {
        const result = await api("/api/coding/dispatch", {method: "POST", body: JSON.stringify({venture_id: ventureId, prompt: text, prefs: [provider]})});
        if (turn !== turnRef.current || capturedOwner.epoch !== verifiedSessionEpoch()
            || capturedOwner.generation !== preparationOwner.current.generation) return;
        if (!result.ok) {
          failureMessage = apiErrorCode(result) === "build_tool_needs_setup"
            ? "Connect your coding tool in Settings > Keys and connections > Coding tools, then retry your build."
            : apiFailure(result, "starting your coding tool");
          throw new Error(failureMessage);
        }
        const order = result.data as {id?: string; venture_id?: string};
        if (!order?.id || !/^[A-Za-z0-9_-]{1,128}$/.test(order.id) || order.venture_id !== ventureId) {
          failureMessage = "Your build receipt could not be verified. Open Builds and refresh your saved work before starting another build.";
          throw new Error(failureMessage);
        }
        setInput("");
        const path = `/v/${encodeURIComponent(ventureId)}/device-work-orders/${encodeURIComponent(order.id)}`;
        if (!routeInChat(path)) window.location.assign(path);
        return;
      }
      if (isComputerUse) {
        const { api } = await import("@/lib/client");
        const { desktopControlRequest } = await import("@/lib/reviewableDeviceRequest");
        const { selectDesktopApplication } = await import("@/lib/desktopApplication");
        const scopeEpoch = verifiedSessionEpoch();
        if (capturedOwner.ventureId !== ventureId || capturedOwner.epoch !== scopeEpoch) return;
        const explicitSurfaces = reviewedSurfaceBody(surfaceReview, ventureId, scopeEpoch);
        if (explicitSurfaces || desktopControlRequest(text)) {
          const status = desktopExecutionAvailability(await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-desktop-pause`));
          if (turn !== turnRef.current || scopeEpoch !== verifiedSessionEpoch()
              || capturedOwner.generation !== preparationOwner.current.generation) return;
          if (status !== "available") throw new Error(desktopExecutionMessage(status));
          const permissions = await desktopInvoke<PermissionSnapshot>("permissions_status");
          if (turn !== turnRef.current || scopeEpoch !== verifiedSessionEpoch()
              || capturedOwner.generation !== preparationOwner.current.generation) return;
          if (!macTaskPermissionsGranted(permissions)) {
            window.dispatchEvent(new CustomEvent(MAC_TASK_PERMISSION_EVENT, { detail: { resume: async () => {
              if (turn !== turnRef.current || scopeEpoch !== verifiedSessionEpoch()
                  || capturedOwner.generation !== preparationOwner.current.generation) return;
              setItems(previous => previous.at(-1)?.kind === "you" && previous.at(-1)?.text === text ? previous.slice(0, -1) : previous);
              await startOrSend(text);
            } } }));
            return;
          }
        }
        const desktopSelection = explicitSurfaces ?? (desktopControlRequest(text)
          ? await selectDesktopApplication(text) : undefined);
        if (turn !== turnRef.current || scopeEpoch !== verifiedSessionEpoch()
            || capturedOwner.generation !== preparationOwner.current.generation) return;
        const prepared = await api(`/api/ventures/${encodeURIComponent(ventureId)}/device-work-orders/from-chat`, {
          method: "POST",
          body: JSON.stringify({ instruction: text, ...desktopSelection }),
        });
        if (turn !== turnRef.current || scopeEpoch !== verifiedSessionEpoch()
            || capturedOwner.generation !== preparationOwner.current.generation) return;
        if (!prepared.ok) {
          const { macTaskPrepareFailure, activeMacTaskReviewPath } = await import("@/lib/macTaskPrepareFailure");
          const reviewPath = activeMacTaskReviewPath(prepared);
          throw new Error(
            reviewPath
              ? `${macTaskPrepareFailure(prepared)} Open ${reviewPath}`
              : macTaskPrepareFailure(prepared),
          );
        }
        const path = (prepared.data as { review_path?: unknown }).review_path;
        if (typeof path !== "string" || !path.startsWith(`/v/${ventureId}/device-work-orders/`)) {
          throw new Error("Supymem could not verify the saved Mac review link.");
        }
        setInput("");
        if (!routeInChat(path)) window.location.assign(path);
        return;
      }
      if (provider === "cursor") {
        const ver = await desktopInvoke<string>("app_version");
        if (!ver) {
          throw new Error(
            "This window cannot reach the Mac app. Reopen Supymem and check for updates in Settings.",
          );
        }
        if (turn !== turnRef.current) return;
        let name = folderName?.trim() || "";
        if (!name) {
          try {
            const { api } = await import("@/lib/client");
            const ven = (await api(`/api/ventures/${ventureId}`)) as { name?: string };
            name = (ven?.name || "").trim();
          } catch {
            /* slug falls back to the venture id in the Mac app */
          }
        }
        if (turn !== turnRef.current) return;
        const started = await desktopInvokeOrThrow<{
          cwd?: string;
          message?: string;
          opened?: boolean;
          order_id?: string;
        }>("start_cursor_ide_session", {
          ventureId,
          prompt: text,
          folderName: name || undefined,
        });
        if (turn !== turnRef.current) return;
        if (started.order_id) {
          const path = `/v/${ventureId}/device-work-orders/${started.order_id}`;
          setInput("");
          if (!routeInChat(path)) window.location.assign(path);
          return;
        }
        setIdeNote(started.message || (started.cwd
          ? `Cursor is open on ${started.cwd}`
          : "Cursor is open. Watch that window."));
        setPhase("running");
        setInput("");
        setItems((prev) => [
          ...prev,
          {
            kind: "status",
            text: started.message || "Cursor is open on this project.",
          },
        ]);
        return;
      }

      if (proto === "acp") {
        let client = clientRef.current;
        if (!client) {
          setPermissions([]);
          client = new AcpSessionClient({ invoke: desktopInvokeOrThrow, listen: desktopListenOrThrow }, event => {
            if (clientRef.current !== client) return;
            if (event.kind === "permission") {
              setPermissions(prev => [...prev, event]);
              onProgressRef.current?.("Your permission is needed for a local action.");
            } else if (event.kind === "activity") {
              if (event.text) setItems(prev => mergeItems(prev, [{ kind: event.toolId ? "tool" : "agent", text: event.text }]));
              if (event.text) onProgressRef.current?.(event.text.slice(0, 72));
            } else {
              setErr(event.message);
              setPermissions([]);
              setBusy(false);
              setPhase("idle");
              onFailedRef.current?.(event.message);
            }
          });
          clientRef.current = client;
          const sid = await client.open(ventureId, provider, () => desktopInvokeOrThrow<string>("venture_workspace_path", { ventureId, folderName: folderName || undefined }));
          if (turn !== turnRef.current) { await client.stop(); return; }
          acpSession.current = sid;
          setCliSession(sid);
          setSessionId(sid);
        }
        setPhase("running");
        setInput("");
        const reason = await client.prompt(text);
        if (turn !== turnRef.current) return;
        setPhase("idle");
        setPermissions([]);
        setItems(prev => [...prev, { kind: "status", text: reason === "end_turn"
          ? "The agent finished its turn. Review the output and run the required checks before treating the work as complete."
          : `The agent stopped (${reason}). Review the session before continuing.` }]);
        return;
      }

      const id = await desktopInvokeOrThrow<string>("spawn_coding_control", {
        provider, ventureId, autoApprove: false, mode: "print", prompt: text, resume: resume || undefined,
      });
      if (turn !== turnRef.current) { await desktopInvokeOrThrow("kill_coding_control", { id }); return; }
      controlIdRef.current = id;
      setSessionId(id);
      setPhase("running");
      setInput("");
    } catch (e) {
      if (turn !== turnRef.current) return;
      if (!isComputerUse && isLocalConsole()) {
        setErr(failureMessage);
        setPhase("idle"); return;
      }
      const client = clientRef.current;
      clientRef.current = null;
      if (client) {
        try { await client.stop(); }
        catch { setIdeNote("The Mac app could not confirm stopping the local process. Review it before starting again."); }
      }
      acpSession.current = null;
      setSessionId(null);
      setPermissions([]);
      const raw = invokeErrorMessage(e, `Couldn’t start ${toolLabel}.`);
      let message = raw;
      if (
        isUnknownDesktopCommand(raw, "start_cursor_ide_session") ||
        isUnknownDesktopCommand(raw, "spawn_coding_control")
      ) {
        setOldApp(true);
        message = `The installed Mac app does not support this command (${raw}). Check for updates in Settings and reopen Supymem.`;
      } else if (/not allowed|denied|acl/i.test(raw)) {
        message = `The website inside the app is not allowed to start ${toolLabel} yet: ${raw}`;
      } else if (/not found|no such file|ENOENT/i.test(raw)) {
        message = isComputerUse
          ? `Couldn’t start ${COMPUTER_USE}. Click Install, then retry.`
          : `${toolLabel} isn’t on this Mac yet. Install it, then click Start again.`;
      }
      setErr(message);
      setInput(text);
      const recoverable =
        /Accessibility|System Settings|Turn on Supymem/i.test(raw) ||
        /Accessibility|System Settings|Turn on Supymem/i.test(message);
      if (!recoverable) onFailedRef.current?.(message);
      setPhase("idle");
    } finally {
      if (turn === turnRef.current) { sendingRef.current = false; setBusy(false); }
    }
  }
  startRef.current = startOrSend;

  async function stop() {
    const client = clientRef.current;
    if (stopping) return;
    if (!client && !sessionId) {
      setErr("Your tool has no tracked running session here. Open Builds in your venture and choose your build to check or stop it.");
      return;
    }
    setStopping(true); setErr("");
    turnRef.current++;
    try {
      if (client) await client.stop();
      else if (sessionId) await desktopInvokeOrThrow("kill_coding_control", { id: sessionId });
      clientRef.current = null;
      setSessionId(null);
      controlIdRef.current = null;
      acpSession.current = null;
      setPermissions([]);
      setPhase("idle");
      setItems(previous => [...previous, {kind: "status", text: "Your coding tool stopped. You can check your saved work or send another request."}]);
      setBusy(false);
      sendingRef.current = false;
    } catch {
      setErr("Your tool has not confirmed Stop. Choose Stop again, or open Builds in your venture to check the running build.");
    } finally { setStopping(false); }
  }

  async function answerPermission(id: RpcId, optionId: string | null) {
    if (!clientRef.current || answeringPermission) return;
    setAnsweringPermission(true);
    try {
      await clientRef.current.answer(id, optionId);
      setPermissions(prev => prev.filter(permission => permission.id !== id));
    } catch (error) {
      setErr(invokeErrorMessage(error, "The permission reply could not be delivered."));
    } finally { setAnsweringPermission(false); }
  }

  return (
    <section className="panel-card" style={{ marginTop: "1rem" }}>
      <h3 style={{ margin: 0 }}>
        {phase === "running"
          ? `${toolLabel} is working`
          : previewUrl
            ? "Preview is live, review it"
            : `${toolLabel} on this Mac`}
      </h3>
      {!isComputerUse && (
        <p className="muted" style={{ marginTop: 8 }}>
          {cliReady
            ? `Using ${toolLabel} on this Mac. I stay attached to this session.`
            : provider === "cursor"
              ? "Cursor should already be installed. Start opens a new window on a dedicated folder for this venture, not the last project you had open."
              : `${toolLabel} should already be installed. Click Start, I will attach and watch the session.`}
        </p>
      )}
      {isComputerUse ? (
        <p className="muted" style={{ marginTop: 8 }}>
          {cliReady
            ? `${COMPUTER_USE} is installed. Execution still requires an authenticated, approved task and a connected device. Review each requested action here.`
            : "For website or code work, open Settings > Keys and connections > Coding tools and connect your tool."}
        </p>
      ) : null}
      {isComputerUse ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
          {[
            "Open Builds in Supymem. Do not modify data.",
            "Check git status in the terminal for the venture workspace.",
            "Inspect the workspace with Cursor, read-only; do not edit files.",
            "Open the browser and check the live site page and waitlist.",
          ].map((example) => (
            <button
              key={example}
              type="button"
              disabled={busy}
              onClick={() => {
                setInput(example);
              }}
              title={example}
              style={{ fontSize: 12 }}
            >
              {example.length > 42 ? `${example.slice(0, 40)}…` : example}
            </button>
          ))}
        </div>
      ) : null}
      {isComputerUse && nativeAvailability !== "available" && <p role="status">{desktopExecutionMessage(nativeAvailability)}</p>}
      {isComputerUse && nativeAvailability === "available" && <DesktopSurfacePicker ventureId={ventureId} disabled={busy} onChange={setSurfaceReview} />}
      {oldApp ? (
        <ErrorNotice error="This Mac app is missing local-tool controls." style={{ color: "var(--danger, #b33)" }} source="agentsession.old_app">
          This Mac app is missing local-tool controls. Quit Supymem and open the latest app from
          the DMG, the website alone cannot start {toolLabel}.
        </ErrorNotice>
      ) : null}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12, alignItems: "center" }}>
        <span className="muted">Local tool actions require individual permission. Installing a tool does not authorize a venture task.</span>
        {sessionId || (isComputerUse && busy) ? (
          <button type="button" data-usage-control="computer_use_stop" className="danger" disabled={stopping} onClick={() => void stop()}>
            {stopping ? "Stopping…" : "Stop"}
          </button>
        ) : null}
      </div>
      {ideNote ? <p className="muted">{ideNote}</p> : null}
      {previewUrl ? (
        <div style={{ marginTop: 12 }}>
          <p className="muted" style={{ margin: "0 0 8px" }}>
            Local preview:{" "}
            <a href={previewUrl} target="_blank" rel="noreferrer">
              {previewUrl}
            </a>
            . Review it here, then click Landing page looks ready.
          </p>
          <iframe
            title="Local landing page preview"
            src={previewUrl}
            sandbox="allow-scripts allow-forms allow-same-origin"
            style={{
              width: "100%",
              height: 520,
              minHeight: 520,
              border: "1px solid var(--border, #ddd)",
              borderRadius: 8,
              background: "#fff",
            }}
          />
        </div>
      ) : null}
      {cliSession ? (
        <p className="muted" style={{ marginTop: 0 }}>
          Your session is saved in {toolLabel}. Open your tool to continue it later.
        </p>
      ) : null}
      <ErrorNotice error={err} source="AgentSessionPane.err" style={{ color: "var(--danger, #b33)" }} />
      {err && isLocalConsole() && !isComputerUse && <a href="/settings/local">Connect your coding tool</a>}
      <div
        ref={logRef}
        style={{
          marginTop: 12,
          maxHeight: 320,
          overflow: "auto",
          padding: 12,
          background: "#f8fafc",
          color: "#17202a",
          fontSize: 13,
          display: "grid",
          gap: 8,
        }}
      >
        {items.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            {isComputerUse
              ? "Ask Supymem to open Builds, check git status in the terminal, inspect the workspace with Cursor, or review the live site and waitlist. Each task has a separate review before it runs."
              : "Type a concrete prompt. Example: “Create a Next.js landing page for this venture in the repo and show me the files you changed.”"}
          </p>
        ) : (
          items.map((item, i) => (
            <div key={`${item.kind}-${i}`}>
              <strong style={{ color: tone(item.kind) }}>{label(item)}</strong> <span style={{overflowWrap: "anywhere"}}>{safeLoopText(item.text, 2000)}</span>
            </div>
          ))
        )}
      </div>
      {permissions.map(permission => (
        <div key={String(permission.id)} role="group" aria-label="Local action permission" style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span>{safeLoopText(permission.title)}</span>
          {permission.options.map(option => (
            <button key={option.optionId} type="button" data-usage-control="computer_use_permission" disabled={answeringPermission}
              className={option.kind === "allow_once" ? "primary" : undefined}
              onClick={() => void answerPermission(permission.id, option.optionId)}>
              {option.kind === "allow_once" ? "Allow once" : "Reject once"}
            </button>
          ))}
          <button type="button" data-usage-control="computer_use_permission" disabled={answeringPermission} onClick={() => void answerPermission(permission.id, null)}>Cancel action</button>
        </div>
      ))}
      <form
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void startOrSend(input);
        }}
        style={{ display: "flex", gap: 8, marginTop: 8 }}
      >
        <input
          aria-label={isComputerUse ? "Tell Charlie what to do" : `Tell ${toolLabel} what to build`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={isComputerUse
            ? "Check git status in the terminal for the venture workspace."
            : `Tell ${toolLabel} what to build…`}
          disabled={busy || (isComputerUse && !cliReady && !oldApp)}
          style={{ flex: 1 }}
        />
        <button type="submit" data-usage-control={isComputerUse ? "computer_use_send" : undefined} className="primary" disabled={busy || !input.trim()}>
          {busy ? "Sending…" : sessionId && acpSession.current ? "Send" : "Start & send"}
        </button>
      </form>
      {isComputerUse && <BrowserProfilePreparation ventureId={ventureId} disabled={busy || phase === "running"} />}
    </section>
  );
}

function mergeItems(prev: TranscriptItem[], next: TranscriptItem[]): TranscriptItem[] {
  const out = [...prev];
  for (const item of next) {
    const last = out[out.length - 1];
    if (item.kind === "agent" && last?.kind === "agent") {
      out[out.length - 1] = { ...last, text: last.text + item.text };
    } else {
      out.push(item);
    }
  }
  return out.slice(-200);
}

function label(item: TranscriptItem): string {
  if (item.kind === "you") return "You";
  if (item.kind === "agent") return "Agent";
  if (item.kind === "thought") return "Thinking";
  if (item.kind === "tool") return item.toolStatus === "completed" ? "Tool done" : "Tool";
  if (item.kind === "error") return "Error";
  return "Status";
}

function tone(kind: TranscriptItem["kind"]): string {
  if (kind === "you") return "var(--fg-0, #17202a)";
  if (kind === "error") return "var(--danger, #c44)";
  if (kind === "tool") return "var(--accent, #1d4ed8)";
  if (kind === "thought") return "var(--fg-2, #64748b)";
  return "var(--fg-1, #334155)";
}

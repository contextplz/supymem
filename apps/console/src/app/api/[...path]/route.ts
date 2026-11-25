import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { isIP } from "node:net";
import { createHmac, timingSafeEqual } from "node:crypto";
import { join, resolve } from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ownerCookie = "supymem_owner";

function matchesCredential(value: string, expected: string): boolean {
  return /^[0-9a-f]{64}$/.test(value) && timingSafeEqual(Buffer.from(value), Buffer.from(expected));
}

function publicRequest(path: string, method: string): boolean {
  path = path.replace(/^\/api\/v1(?=\/)/, "");
  return (["GET", "POST"].includes(method) && (["/public/unsubscribe", "/public/email/unsubscribe"].includes(path)
    || /^\/public\/waitlists\/[A-Za-z0-9_-]{1,128}$/.test(path)
    || /^\/public\/waitlists\/[A-Za-z0-9_-]{1,128}\/surveys\/exp_[a-f0-9]{24}\/[A-Za-z0-9_-]{20,128}$/.test(path)))
    || (method === "GET" && /^\/public\/waitlists\/[A-Za-z0-9_-]{1,128}\/pricing\/exp_[a-f0-9]{24}\/[a-z][a-z0-9_]{0,19}$/.test(path))
    || (method === "GET" && (path === "/ai-gtm/gmail/callback" || /^\/content\/oauth\/[A-Za-z0-9_-]+\/callback$/.test(path)))
    || (method === "POST" && path === "/e");
}

function localAddress(value: string): URL | null {
  try {
    const address = new URL(value);
    if (!["http:", "https:"].includes(address.protocol) || address.username || address.password
      || address.pathname !== "/" || address.search || address.hash) return null;
    return address.hostname === "localhost" || address.hostname === "[::1]"
      || (isIP(address.hostname) === 4 && address.hostname.startsWith("127.")) ? address : null;
  } catch {
    return null;
  }
}

async function proxy(request: Request): Promise<Response> {
  if (process.env.SUPYMEM_MODE?.trim().toLowerCase() !== "local"
    && process.env.NEXT_PUBLIC_SUPYMEM_MODE?.trim().toLowerCase() !== "local") {
    return Response.json({ error: "Open your hosted app to continue." }, { status: 404 });
  }
  const url = new URL(request.url);
  const path = url.pathname === "/api/v1" || url.pathname.startsWith("/api/v1/") ? url.pathname : url.pathname.slice(4);
  const publicRoute = publicRequest(path, request.method);
  const consoleOrigin = (process.env.CONSOLE_ORIGIN || process.env.CONSOLE_URL
    || `http://127.0.0.1:${process.env.CONSOLE_PORT || "3100"}`).replace(/\/$/, "");
  const host = localAddress(`${url.protocol}//${request.headers.get("host") || ""}`);
  const origin = request.headers.get("origin");
  const referer = request.headers.get("referer");
  const allowedOrigins = [consoleOrigin, "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"];
  const remotePublic = publicRoute && ["1", "true", "yes", "on"].includes(process.env.SUPYMEM_LOCAL_ALLOW_REMOTE || "")
    && url.origin === process.env.COMPANY_OS_PUBLIC_URL?.replace(/\/$/, "")
    && request.headers.get("host") === url.host;
  let validReferer = true;
  try {
    if (referer !== null) validReferer = allowedOrigins.includes(new URL(referer).origin);
  } catch { validReferer = false; }
  if ((!remotePublic && (!host || host.origin !== consoleOrigin || url.port !== new URL(consoleOrigin).port))
    || (!publicRoute && ((origin !== null && !allowedOrigins.includes(origin)) || !validReferer
      || request.headers.get("sec-fetch-site") === "cross-site"
      || (request.headers.get("sec-fetch-site") === "same-site" && origin === null && referer === null)))) {
    return Response.json({ error: "Open your app on your own machine, then retry." }, { status: 403 });
  }
  try {
    const headers = new Headers(request.headers);
    const hopHeaders = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"];
    const connectionHeaders = headers.get("connection")?.split(",").map(value => value.trim()) || [];
    for (const key of [...hopHeaders, ...connectionHeaders, "host", "content-length", "cookie", "x-local-proxy-secret", "x-local-session-proof", "x-local-owner-proof", "x-forwarded-for", "x-forwarded-host", "forwarded"]) headers.delete(key);
    headers.set("x-forwarded-host", url.host);
    if (publicRoute) {
      for (const key of ["x-subject", "x-cos-assertion", "authorization", "x-api-key", "x-workspace-id"]) headers.delete(key);
    }
    if (!publicRoute) {
      const filename = process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE
        || join(process.env.COMPANY_OS_DATA_DIR || resolve(process.cwd(), "../../data"), "owner-session.secret");
      try {
        const file = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
        try {
          const metadata = await file.stat();
          const secret = (await file.readFile("utf8")).trim();
          if (!metadata.isFile() || (process.platform !== "win32" && (metadata.mode & 0o077
            || metadata.uid !== process.geteuid?.())) || !/^[0-9a-f]{64}$/.test(secret)) throw new Error();
          const cookies = (request.headers.get("cookie") || "").split(";").map(value => value.trim())
            .filter(value => value.startsWith(`${ownerCookie}=`)).map(value => value.slice(ownerCookie.length + 1));
          const credential = request.headers.get("x-local-proxy-secret") || "";
          const proof = request.headers.get("x-local-session-proof") || "";
          const cookieValue = createHmac("sha256", secret).update(proof).digest("hex");
          const documentProof = createHmac("sha256", secret).update(`local-document-proof:${consoleOrigin}`).digest("hex");
          const documentCookie = createHmac("sha256", secret).update(`local-document-session:${consoleOrigin}:${documentProof}`).digest("hex");
          const browserSession = /^[0-9a-f]{64}$/.test(proof) && cookies.length === 1
            && (matchesCredential(cookies[0], cookieValue)
              || (matchesCredential(proof, documentProof) && matchesCredential(cookies[0], documentCookie)));
          if (cookies.length > 1 || !(matchesCredential(credential, secret) || browserSession)) {
            return Response.json({ error: { code: "local_owner_required",
              message: `Open your app at ${consoleOrigin} on this machine, then retry.` } },
              { status: 403, headers: { "Cache-Control": "no-store" } });
          }
          if (path === "/local/owner-session" && request.method === "POST") {
            if (!/^[0-9a-f]{64}$/.test(proof)) return Response.json({ error: "Restart your desktop app, then reopen your workspace and retry." }, { status: 403 });
            return Response.json({ ok: true }, { headers: {
              "Cache-Control": "no-store",
              "Set-Cookie": `${ownerCookie}=${cookieValue}; Path=/api; HttpOnly; SameSite=Strict${url.protocol === "https:" ? "; Secure" : ""}`,
            } });
          }
          headers.set("x-local-proxy-secret", secret);
        } finally {
          await file.close();
        }
      } catch {
        return Response.json({ error: { code: "local_proxy_not_ready", message: "Check your app data-folder permissions, then restart your app. If this keeps happening, close your app and remove its owner-session.secret file before reopening." } },
          { status: 503, headers: { "Cache-Control": "no-store" } });
      }
    }
    const core = (process.env.CORE_URL || "http://127.0.0.1:3300").replace(/\/$/, "");
    const init: RequestInit & { duplex?: "half" } = {
      method: request.method, headers, signal: request.signal, cache: "no-store", redirect: "error",
      ...(request.body ? { body: request.body, duplex: "half" } : {}),
    };
    const response = await fetch(`${core}${path}${url.search}`, init);
    const responseHeaders = new Headers(response.headers);
    for (const key of [...hopHeaders, "content-length", "content-encoding", "x-local-proxy-secret", "x-local-session-proof", "x-local-owner-proof", "set-cookie"]) responseHeaders.delete(key);
    responseHeaders.set("cache-control", "no-store");
    return new Response(response.body, { status: response.status, headers: responseHeaders });
  } catch {
    return Response.json({ error: { code: "core_unavailable", message: "Your app could not be reached. Restart your app, then retry." } },
      { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE, proxy as HEAD, proxy as OPTIONS };

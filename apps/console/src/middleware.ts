import { NextResponse, type NextRequest } from "next/server";
import { isBetaHiddenPath } from "@/lib/betaRoutes";
import { isHostedOnlyPath, localHomeHref } from "@/lib/localMode";
import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { createHmac } from "node:crypto";
import { join, resolve } from "node:path";

/** Beta: routes outside the golden path are not reachable by URL. */
export async function middleware(request: NextRequest) {
  const local = process.env.SUPYMEM_MODE?.trim().toLowerCase() === "local"
    || process.env.NEXT_PUBLIC_SUPYMEM_MODE?.trim().toLowerCase() === "local";
  const headers = new Headers(request.headers);
  headers.delete("x-local-owner-proof");
  if (local
    && !["1", "true", "yes", "on"].includes(process.env.SUPYMEM_LOCAL_ALLOW_REMOTE?.trim().toLowerCase() || "")) {
    try {
      for (const name of ["host", "x-forwarded-host"]) {
        const authority = request.headers.get(name);
        if (name === "x-forwarded-host" && authority === null) continue;
        if (!authority || /[\u0000-\u0020\u007f\\/@?#]/.test(authority)) throw new Error();
        const parsed = new URL(`http://${authority}`);
        if (parsed.username || parsed.password || parsed.port === "0"
          || !(parsed.hostname === "localhost" || /^127\.\d+\.\d+\.\d+$/.test(parsed.hostname)
            || parsed.hostname === "[::1]" || /^\[::ffff:7f[\da-f]{2}:[\da-f]{1,4}\]$/.test(parsed.hostname))) throw new Error();
      }
    } catch {
      return new NextResponse("Open your local app directly and try again.", { status: 403, headers: { "Cache-Control": "no-store" } });
    }
  }
  if (local && isHostedOnlyPath(request.nextUrl.pathname)) {
    return NextResponse.redirect(new URL(localHomeHref(request.nextUrl.pathname,
      request.nextUrl.searchParams.get("venture")), request.url));
  }
  if (!local && isBetaHiddenPath(request.nextUrl.pathname)) return new NextResponse("Not found", { status: 404 });
  const response = NextResponse.next({ request: { headers } });
  if (local && ["GET", "HEAD"].includes(request.method)
    && !request.nextUrl.pathname.startsWith("/api/") && request.nextUrl.pathname !== "/api"
    && request.headers.get("accept")?.includes("text/html")) {
    const consoleOrigin = (process.env.CONSOLE_ORIGIN || process.env.CONSOLE_URL
      || `http://127.0.0.1:${process.env.CONSOLE_PORT || "3100"}`).replace(/\/$/, "");
    try {
      const url = new URL(request.url);
      const host = request.headers.get("host");
      const forwarded = request.headers.get("x-forwarded-host");
      const origin = request.headers.get("origin");
      const referer = request.headers.get("referer");
      // Next rewrites request.url to localhost for loopback hosts, so the Host header (not url.origin) carries the exact origin.
      if (url.port !== new URL(consoleOrigin).port || `${url.protocol}//${host}` !== consoleOrigin
        || !(url.hostname === "localhost" || url.hostname === "[::1]" || /^127\.\d+\.\d+\.\d+$/.test(url.hostname))
        || (forwarded !== null && forwarded !== host)
        || (origin !== null && origin !== consoleOrigin)
        || (referer !== null && new URL(referer).origin !== consoleOrigin)
        || ![null, "none", "same-origin"].includes(request.headers.get("sec-fetch-site"))
        || ![null, "document"].includes(request.headers.get("sec-fetch-dest"))) throw new Error();
    } catch {
      return new NextResponse("Open your app at its local address on this machine, then retry.", {
        status: 403, headers: { "Cache-Control": "no-store" },
      });
    }
    try {
      const filename = process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE
        || join(process.env.COMPANY_OS_DATA_DIR || resolve(process.cwd(), "../../data"), "owner-session.secret");
      const file = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const metadata = await file.stat();
        const secret = (await file.readFile("utf8")).trim();
        if (!metadata.isFile() || (process.platform !== "win32" && (metadata.mode & 0o077
          || metadata.uid !== process.geteuid?.())) || !/^[0-9a-f]{64}$/.test(secret)) throw new Error();
        const proof = createHmac("sha256", secret).update(`local-document-proof:${consoleOrigin}`).digest("hex");
        headers.set("x-local-owner-proof", proof);
        const owned = NextResponse.next({ request: { headers } });
        owned.headers.set("Cache-Control", "no-store");
        owned.headers.set("Set-Cookie", `supymem_owner=${createHmac("sha256", secret).update(`local-document-session:${consoleOrigin}:${proof}`).digest("hex")}; Path=/api; HttpOnly; SameSite=Strict${consoleOrigin.startsWith("https:") ? "; Secure" : ""}`);
        return owned;
      } finally { await file.close(); }
    } catch {
      return new NextResponse("Check your app data-folder permissions, then restart your app and reopen its local address.", {
        status: 503, headers: { "Cache-Control": "no-store" },
      });
    }
  }
  return response;
}

export const config = {
  runtime: "nodejs",
  matcher: ["/((?!_next/|favicon.ico).*)"],
};

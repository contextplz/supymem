import { createHmac } from "node:crypto";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET(request: Request): Response {
  const headers = new Headers({ "Cache-Control": "no-store" });
  if (process.env.SUPYMEM_MODE?.trim().toLowerCase() !== "local") return new Response(null, { status: 404, headers });
  const port = process.env.PORT || process.env.CONSOLE_PORT || "3100";
  if (request.headers.get("host") !== `127.0.0.1:${port}` || request.headers.has("origin")) {
    return new Response(null, { status: 403, headers });
  }
  const key = process.env.SUPYMEM_LOCAL_READINESS_KEY;
  if (key !== undefined) {
    const nonce = request.headers.get("x-local-ready-nonce") || "";
    if (!/^[0-9a-f]{32}$/.test(nonce)) return new Response(null, { status: 403, headers });
    headers.set("X-Local-Ready-Proof", createHmac("sha256", key).update(nonce).digest("hex"));
  }
  return new Response(null, { status: 204, headers });
}

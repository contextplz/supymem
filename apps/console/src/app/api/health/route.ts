import { NextResponse } from "next/server";
import { GET as proxy } from "../[...path]/route";

export const dynamic = "force-dynamic";

export function GET(request: Request) {
  if (process.env.SUPYMEM_MODE?.trim().toLowerCase() === "local") return proxy(request);
  return NextResponse.json({ ok: true, buildId: process.env.NEXT_PUBLIC_CONSOLE_BUILD_ID,
    ...(process.env.SUPYMEM_MODE?.trim().toLowerCase() === "local" && process.env.SUPYMEM_DESKTOP_INSTANCE_ID
      ? { desktop_instance: process.env.SUPYMEM_DESKTOP_INSTANCE_ID } : {}),
  }, {
    headers: { "Cache-Control": "no-store" },
  });
}

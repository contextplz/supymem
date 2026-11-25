import type { ReactNode } from "react";
import { headers } from "next/headers";
import "@/styles/tokens.css";

import { ChunkReloadGuard } from "@/components/desktop/ChunkReloadGuard";
import { ConsoleBuildUpdate } from "@/components/desktop/ConsoleBuildUpdate";
import { DesktopBoot } from "@/components/desktop/DesktopBoot";
import { DesktopUpdateGate } from "@/components/desktop/DesktopUpdateGate";
import { DesktopProductBoundary } from "@/components/desktop/DesktopProductBoundary";
import { CharlieIntro } from "@/components/desktop/CharlieIntro";
import { LocalModeBoundary } from "@/components/layout/LocalModeBoundary";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "Supymem",
  description: "A team of AI teammates that runs your venture, with a shared brain",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const local = process.env.SUPYMEM_MODE?.trim().toLowerCase() === "local"
    || process.env.NEXT_PUBLIC_SUPYMEM_MODE?.trim().toLowerCase() === "local";
  const proof = local ? (await headers()).get("x-local-owner-proof") : null;
  const bootstrap = /^[0-9a-f]{64}$/.test(proof || "") ? `(() => { const proof = ${JSON.stringify(proof)}; const origin = location.origin; const original = window.fetch.bind(window); window.fetch = (input, options) => { const request = new Request(input, options); const address = new URL(request.url); if (address.origin !== origin || !address.pathname.startsWith('/api/')) return original(request); const headers = new Headers(request.headers); headers.set('X-Local-Session-Proof', proof); return original(new Request(request, { headers })); }; })();` : "";
  return (
    <html lang="en">
      <body className={local ? "local-app-frame" : undefined}>
        {local && <script id="local-owner-session" dangerouslySetInnerHTML={{ __html: "window.__SUPYMEM__={...window.__SUPYMEM__,mode:'local'};" + bootstrap }} />}
        <ChunkReloadGuard />
        <DesktopBoot />
        <LocalModeBoundary local={local}>
          {local ? children : <>
            <DesktopUpdateGate />
            <DesktopProductBoundary><ConsoleBuildUpdate />{children}</DesktopProductBoundary>
            <CharlieIntro />
          </>}
        </LocalModeBoundary>
      </body>
    </html>
  );
}

import type { NextConfig } from "next";
import { randomUUID } from "node:crypto";

// Build workers inherit this value; the client and health route bake in one ID.
const buildId = process.env.CONSOLE_BUILD_ID ||= randomUUID();
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${process.env.NODE_ENV === "development" ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com data:",
  "img-src 'self' https: data: blob: asset: http://asset.localhost",
  "media-src 'self' https: data: blob:",
  `connect-src 'self' https://api.openai.com blob: ipc: http://ipc.localhost`,
  "frame-src 'self' https: http: data: blob:",
  "worker-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "report-uri /api/csp-report",
].join("; ");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  // Desktop packaging (doc 90 §4 v1): standalone Node server as a sidecar.
  output: "standalone",
  // No next/image use: keep the image optimizer (and its sharp/libvips surface) off.
  images: { unoptimized: true },
  generateBuildId: async () => buildId,
  env: { NEXT_PUBLIC_CONSOLE_BUILD_ID: buildId, NEXT_PUBLIC_SUPYMEM_MODE: "local" },
  headers: async () => [{
    source: "/:path*",
    headers: [
      { key: "Content-Security-Policy-Report-Only", value: csp },
      { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    ],
  }],
};

export default nextConfig;

"use client";

import { useRouter } from "next/navigation";
import { MacPermissionsSetup } from "@/components/desktop/MacPermissionsSetup";

/** First-run Mac permissions; also reachable from Mac app settings. */
export default function SetupPermissionsPage() {
  const router = useRouter();
  return <MacPermissionsSetup onContinue={() => router.replace("/")} />;
}

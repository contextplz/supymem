import { notFound } from "next/navigation";
import { LocalProviderGate } from "./LocalSetup";

export const dynamic = "force-dynamic";

export default function SetupPage() {
  if (process.env.SUPYMEM_MODE?.trim().toLowerCase() !== "local" && process.env.NEXT_PUBLIC_SUPYMEM_MODE?.trim().toLowerCase() !== "local") notFound();
  return <LocalProviderGate><main><h1>You are ready</h1><a href="/">Open your workspace</a></main></LocalProviderGate>;
}

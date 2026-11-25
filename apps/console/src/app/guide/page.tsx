import { GuidePage } from "@/components/guide/CapabilityGuide";

export const metadata = { title: "How to use Supymem | Supymem" };

export default async function Page({ searchParams }: { searchParams?: Promise<{ venture?: string | string[] }> }) {
  const query = await searchParams;
  const selected = typeof query?.venture === "string" ? query.venture.trim() : undefined;
  // A navigation selector only. AppShell and the destination APIs retain account authorization.
  const ventureId = selected && /^ven_[A-Za-z0-9_-]{1,124}$/.test(selected) ? selected : undefined;
  return <GuidePage ventureId={ventureId} />;
}

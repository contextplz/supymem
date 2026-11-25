import { GuidePage } from "@/components/guide/CapabilityGuide";

export const metadata = { title: "How to use Supymem | Supymem" };

export default async function Page({ params }: { params: Promise<{ ventureId: string }> }) {
  const { ventureId } = await params;
  return <GuidePage ventureId={ventureId} />;
}

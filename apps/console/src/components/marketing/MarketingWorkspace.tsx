"use client";

import Link from "@/components/chat/ChatLink";
import { useOperationsScope } from "@/components/ops/OpsWorkspaceGate";
import { operationsHref } from "@/lib/operationsScope";

const WORKFLOWS = [
  {
    href: "/gtm",
    title: "Choose the first customer motion",
    body: "Turn the company plan into a focused diagnosis and the next test for reaching a paid user.",
    action: "Open go-to-market",
  },
  {
    href: "/content",
    title: "Prepare founder-reviewed content",
    body: "Draft useful content from the confirmed company context. Nothing publishes without review.",
    action: "Open content",
  },
  {
    href: "/ops/outreach",
    title: "Prepare the first conversations",
    body: "Define the people to approach, add real evidence, and review drafts. LinkedIn messages remain manual.",
    action: "Open outreach",
  },
  {
    href: "/media",
    title: "Create product assets",
    body: "Use real product screenshots, logos, and founder-owned media for reviewed campaign assets.",
    action: "Open media",
  },
  {
    href: "/community",
    title: "Decide whether community fits",
    body: "Evaluate the evidence before creating a community, then connect Discord only when it is useful.",
    action: "Open community",
  },
  {
    href: "/ads",
    title: "Prepare a paid campaign",
    body: "Create an approval-backed campaign blueprint. Provider campaigns always start paused.",
    action: "Open ads",
  },
] as const;

export function MarketingWorkspace() {
  const scope = useOperationsScope();

  return (
    <div className="chat-scroll">
      <div className="chat-inner">
        <section className="panel-card">
          <p className="eyebrow">FROM EVIDENCE TO FIRST CUSTOMER</p>
          <h2 style={{ marginTop: 6 }}>Choose the next useful marketing move</h2>
          <p className="muted" style={{ maxWidth: 760 }}>
            Each workspace below uses this company&apos;s confirmed idea and plan. Connections are optional until a
            workflow needs them, and Supymem will show the exact founder action at that point.
          </p>
          <Link className="primary" href={operationsHref("/connections", scope)}>
            Review optional connections
          </Link>
        </section>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 16,
          }}
        >
          {WORKFLOWS.map((workflow) => (
            <section className="panel-card" key={workflow.href}>
              <h3 style={{ marginTop: 0 }}>{workflow.title}</h3>
              <p className="muted" style={{ minHeight: 66 }}>{workflow.body}</p>
              <Link href={operationsHref(workflow.href, scope)}>{workflow.action} →</Link>
            </section>
          ))}
        </div>

        <section className="panel-card">
          <h3 style={{ marginTop: 0 }}>Founder control</h3>
          <p className="muted">
            Research, drafting, and analysis can run in the background. Sending messages, publishing content,
            enabling ads, and spending money remain explicit founder decisions with a visible approval record.
          </p>
        </section>
      </div>
    </div>
  );
}

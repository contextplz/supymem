import { BETA_PATH } from "@/lib/betaPath";

/** The beta path with what happens next. `current` highlights a step; null shows the whole path. */
export function BetaPathStrip({ current = null, detail = false }: { current?: number | null; detail?: boolean }) {
  return (
    <ol className="beta-path" aria-label="Your path: Idea, Landing page, Publish, Waitlist, Outreach, Research">
      {BETA_PATH.map((step, index) => {
        const done = current !== null && index < current;
        const active = current === index;
        return (
          <li key={step.key} aria-current={active ? "step" : undefined} className="beta-path-step"
            style={{ border: "1px solid var(--line, #dbe2eb)", borderRadius: 10, padding: "10px 12px", opacity: done ? 0.65 : 1,
              background: active ? "var(--bg-1, #f6f8fb)" : "transparent", borderColor: active ? "var(--accent, #2563eb)" : undefined }}>
            <strong>{done ? "✓ " : `${index + 1}. `}{step.label}</strong>
            <span className="muted" style={{ display: "block", fontSize: "0.85rem" }}>{step.time}</span>
            {detail && <span className="muted" style={{ display: "block", fontSize: "0.85rem", marginTop: 4 }}>{step.what}</span>}
          </li>
        );
      })}
    </ol>
  );
}

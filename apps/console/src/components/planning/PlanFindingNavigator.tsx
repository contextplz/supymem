import { useState, type ReactNode } from "react";
import { findingInSection } from "../../lib/planReview.ts";

type Finding = { code: string; path: string; message: string };

export function PlanFindingNavigator({ findings, note, className, onJump }: {
  findings: Finding[]; note: string; className?: string; onJump: (path: string) => void;
}) {
  if (!findings.length) return null;
  return <div className={className}><h3>Jump to a flagged location</h3><ul>{findings.map((finding, index) => <li key={`${finding.code}-${finding.path}-${index}`}>
    <button type="button" onClick={() => onJump(finding.path)}>Location {index + 1}: {finding.path}</button><span>{finding.message}</span>
  </li>)}</ul>{note && <p role="status">{note}</p>}</div>;
}

export function PlanEditSection({ path, title, findingPaths, className, children }: {
  path: string; title: string; findingPaths: string[]; className?: string; children: ReactNode;
}) {
  const flagged = findingPaths.some(finding => findingInSection(finding, path));
  const [open, setOpen] = useState(flagged);
  return <details className={className} open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>{title}{flagged ? " · Needs review" : ""}</summary>{children}</details>;
}

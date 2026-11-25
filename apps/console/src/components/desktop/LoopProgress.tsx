import { loopIterationLabel, safeLoopText, safeChangedFiles, type ChangedFile, type LoopEvent, type LoopIteration } from "@/lib/loopProgress";

export function LoopProgress({ maximum, events = [], iterations = [], files, compact = false, collapsed = false, onOpenActivity }: {
  maximum: number; events?: LoopEvent[]; iterations?: LoopIteration[]; files?: ChangedFile[];
  compact?: boolean; collapsed?: boolean; onOpenActivity?: () => void;
}) {
  const changed = safeChangedFiles(files);
  const activity = events.length ? <ol aria-live="polite" aria-relevant="additions">
    {(collapsed ? events : events.slice(compact ? -5 : -50)).map((event, index) => <li key={`${event.at}:${index}`}>
      {collapsed && /^Running:/i.test(event.text) ? "Your tool ran a command." : safeLoopText(event.text, 280)}
    </li>)}
  </ol> : <p role="status">Your tool has not sent an activity update yet. Keep this screen open to watch your build.</p>;
  const changedFiles = changed.length ? <ul>{changed.map(file => <li key={file.path}><code>{file.path}</code>{file.deleted ? " (removed)" : ""}</li>)}</ul>
      : <p>{files ? "No project files have changed yet. Keep watching for updates."
        : "Your tool has not reported changed files yet. Open your result folder to inspect the saved files."}</p>;
  const savedTries = <ol>
      {iterations.slice(0, maximum + 1).map(row => <li key={row.index}>
        <strong>{loopIterationLabel(row, maximum)}</strong>
        {row.summary && <p>{safeLoopText(row.summary, 280)}</p>}
        {row.diagnosis?.failures?.length ? <ul>{row.diagnosis.failures.slice(0, 10).map((failure, index) =>
          <li key={index}>{safeLoopText(typeof failure === "string" ? failure
            : failure && typeof failure === "object" && "message" in failure ? failure.message : "A check needs a fix. Review your saved result folder, then retry.")}</li>)}</ul> : null}
      </li>)}
    </ol>;
  return <section aria-label="Coding progress" style={{ overflowWrap: "anywhere" }}>
    {collapsed ? <>
      <details><summary>Files changed</summary>{changedFiles}</details>
      <details><summary>Saved tries</summary>{iterations.length ? savedTries : <p>No tries have been saved yet. Choose Refresh build to check again.</p>}</details>
      <details onToggle={onOpenActivity && (event => { if (event.currentTarget.open) onOpenActivity(); })}>
        <summary>Full activity log</summary>{activity}</details>
    </> : <>
      {events.length > 0 && <h2>What's happening</h2>}{activity}
      {!compact && <><h2>Files changed</h2>{changedFiles}</>}
      {iterations.length > 0 && <><h2>Saved tries</h2>{savedTries}</>}
    </>}
  </section>;
}

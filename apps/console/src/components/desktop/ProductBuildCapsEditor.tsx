import { useState, type FormEvent } from "react";
import { parseProductBuildCaps, type ProductBuildCaps, type ProductBuildPhase } from "@/lib/productBuildCaps";
import { ErrorNotice } from "@/components/primitives/ErrorNotice";

export function ProductBuildCapsEditor({ caps, provider, phase, disabled, freshReview, onDirtyChange, onPrepare,
  className, primaryClassName }: {
  caps: ProductBuildCaps; provider: string; phase: ProductBuildPhase; disabled: boolean; freshReview: boolean;
  onDirtyChange?: (dirty: boolean) => void; onPrepare: (caps: ProductBuildCaps) => Promise<void>;
  className?: string; primaryClassName?: string;
}) {
  const initial = { max_iterations: String(caps.max_iterations), max_wall_seconds: String(caps.max_wall_seconds),
    per_call_seconds: String(caps.per_call_seconds), max_turns: caps.max_turns == null ? "" : String(caps.max_turns) };
  const [draft, setDraft] = useState(initial);
  const dirty = Object.keys(initial).some(key => draft[key as keyof typeof draft] !== initial[key as keyof typeof initial]);
  const selected = parseProductBuildCaps({ max_iterations: Number(draft.max_iterations),
    max_wall_seconds: Number(draft.max_wall_seconds), per_call_seconds: Number(draft.per_call_seconds),
    max_turns: provider === "claude" ? Number(draft.max_turns) : null }, provider, phase);
  function change(next: typeof draft) {
    setDraft(next);
    onDirtyChange?.(Object.keys(initial).some(key => next[key as keyof typeof next] !== initial[key as keyof typeof initial]));
  }
  const fields: Array<{ key: keyof typeof draft; label: string; max: number; control: string }> = [
    { key: "max_iterations", label: "Number of tries", max: phase === "plan" ? 2 : 10, control: "product_build_limit_tries" },
    { key: "max_wall_seconds", label: "Total time limit in seconds", max: 5400, control: "product_build_limit_total" },
    { key: "per_call_seconds", label: "Time per try in seconds", max: 1200, control: "product_build_limit_call" },
    ...(provider === "claude" ? [{ key: "max_turns" as const, label: "Claude turns per try", max: 60, control: "product_build_limit_turns" }] : []),
  ];
  function submit(event: FormEvent) {
    event.preventDefault();
    if (selected && !disabled && (freshReview || dirty)) void onPrepare(selected);
  }
  return <form onSubmit={submit} className={className} aria-label={phase === "plan" ? "Build plan limits" : "Coding run limits"}>
    <fieldset disabled={disabled}>
      <legend>{phase === "plan" ? "Set build plan limits" : "Set coding limits"}</legend>
      <p>{freshReview ? "These limits will be saved in a new review for you to approve."
        : "Saving new limits cancels this unstarted review and prepares a new one. Review it again before approving."}</p>
      {fields.map(field => <label key={field.key}>
        {field.label}
        <input type="number" min={1} max={field.max} step={1} required
          data-usage-control={field.control} value={draft[field.key]}
          onChange={event => change({ ...draft, [field.key]: event.target.value })} />
      </label>)}
      {!selected && <ErrorNotice source="product-build.caps.validation"
        error="Choose whole numbers within the limits. Time per try must not exceed the total time." />}
      {dirty && !freshReview && <p role="status">The changed limits are not saved yet. Prepare the new review before approving.</p>}
      <button type="submit" className={primaryClassName} data-usage-control={phase === "plan" ? "product_build_limits_save" : "product_build_run_review"}
        disabled={disabled || !selected || (!freshReview && !dirty)}>
        {phase === "plan" ? "Prepare review with these limits" : "Review coding limits"}
      </button>
      {dirty && !freshReview && <button type="button" data-usage-control="product_build_limits_reset"
        disabled={disabled} onClick={() => change(initial)}>Keep saved limits</button>}
    </fieldset>
  </form>;
}

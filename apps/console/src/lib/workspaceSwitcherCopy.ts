/** Switcher eyebrow + label from path/query shell id and resolved name. */
export function workspaceSwitcherCopy(currentId: string | undefined, resolvedName: string | undefined): {
  eyebrow: "Venture" | "Ventures";
  label: string;
} {
  if (!currentId) return { eyebrow: "Ventures", label: "All ventures" };
  return { eyebrow: "Venture", label: resolvedName?.trim() || "Loading venture…" };
}

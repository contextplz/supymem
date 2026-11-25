export type NamedDesktopApp = { name: string; bundle_id: string };

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Only a leading location clause may nominate an app for native control. */
export function leadingDesktopAppScope(instruction: string): { clause: string; remainder: string } | null {
  const match = /^\s*(?:in|inside|within|on)\s+([^,]{1,100}),\s*/i.exec(instruction);
  if (!match) return null;
  const clause = match[1].trim();
  if (/^(?:this|my|the)\s+mac$/i.test(clause)) return null;
  return { clause, remainder: instruction.slice(match[0].length) };
}

function scopeNamesApp(clause: string, appName: string): boolean {
  const scope = clause.toLowerCase().replace(/’/g, "'").replace(/\s+on\s+this\s+mac$/i, "").trim();
  const name = appName.toLowerCase().trim();
  const escaped = escapeRegExp(name);
  return scope === name
    || new RegExp(`^${escaped}'(?:s\\b|(?=\\s))`).test(scope)
    || new RegExp(`\\b(?:in|of)\\s+${escaped}$`).test(scope);
}

/** A requested app can also appear in the task's output text or description. */
export function chooseNamedDesktopApp<T extends NamedDesktopApp>(instruction: string, installed: T[]): T | null {
  const named = installed.filter((app) => {
    const name = app.name.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
    return name.length >= 3 && new RegExp(`\\b${escapeRegExp(name)}\\b`, "i").test(instruction);
  });
  const leadingScope = leadingDesktopAppScope(instruction)?.clause;
  const conditionalScope = /^\s*if\s+([a-z][a-z0-9 .'-]{1,50}?)\s+(?:is|has|shows|displays)\b/i.exec(instruction)?.[1]
    ?.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
  if (leadingScope || conditionalScope) {
    // The leading scope is authoritative. An unavailable app or multiple
    // installed app mentions must not fall back to an incidental app name.
    const scoped = installed.filter((app) => leadingScope
      ? scopeNamesApp(leadingScope, app.name)
      : app.name.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim() === conditionalScope);
    return scoped.length === 1 && named.length === 1 ? scoped[0] : null;
  }
  const requested = named.filter((app) => {
    const name = app.name.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
    return new RegExp(`\\b(?:open|launch|start|use|control|inspect|switch to)\\s+(?:(?:my|the)\\s+)?${escapeRegExp(name)}\\b`, "i").test(instruction);
  });
  if (requested.length === 1) return requested[0];
  if (requested.length > 1) return null;
  // An incidental mention of a known app must not become the target when the
  // founder explicitly requested a different app absent from the inventory.
  if (/\b(?:open|launch|start|use|control|inspect|switch to)\s+(?:my\s+|the\s+)?[a-z]/i.test(instruction)) {
    return null;
  }
  return named.length === 1 ? named[0] : null;
}

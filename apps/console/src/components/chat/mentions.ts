import { prettyName, prettyRole } from "@/components/office/avatars";

export type MentionAgent = {
  id: string;
  name?: string;
  role?: string;
  title?: string;
};

export type MentionHit = {
  id: string;
  insert: string;
  name: string;
  role: string;
  label: string;
};

function token(value: string | undefined): string {
  return (value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function mentionQuery(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const at = before.lastIndexOf("@");
  if (at < 0) return null;
  if (at > 0 && /[\w]/.test(before[at - 1] || "")) return null;
  const query = before.slice(at + 1);
  if (query.includes(" ") || query.includes("\n")) return null;
  return { start: at, query };
}

export function filterMentions(agents: MentionAgent[], query: string): MentionHit[] {
  const needle = token(query);
  if (needle.length < 2) return [];
  const hits: MentionHit[] = [];
  const seen = new Set<string>();
  for (const agent of agents) {
    const name = prettyName(agent.name || agent.role || "Teammate");
    const role = prettyRole(agent.role || agent.title || "");
    const aliases = [token(agent.name), token(agent.role), token(agent.title), token(role)];
    const match = aliases.some(
      (alias) => alias && (alias === needle || alias.startsWith(needle) || needle.startsWith(alias)),
    );
    if (!match || seen.has(agent.id)) continue;
    seen.add(agent.id);
    hits.push({
      id: agent.id,
      insert: `@${name.replace(/\s+/g, "")}`,
      name,
      role,
      label: role ? `${name} · ${role}` : name,
    });
    const roleInsert = role ? `@${role.replace(/\s+/g, "")}` : "";
    if (roleInsert && token(role).startsWith(needle) && !hits.some((h) => h.insert === roleInsert)) {
      hits.push({
        id: `${agent.id}-role`,
        insert: roleInsert,
        name,
        role,
        label: `${role} · ${name}`,
      });
    }
  }
  return hits.slice(0, 8);
}

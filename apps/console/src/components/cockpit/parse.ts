import type { ProvenanceMode } from "@/components/primitives/ProvenanceChip";
import type {
  AgentLiveItem,
  ChatMessage,
  DashboardPayload,
  MoneyPayload,
  OrgAgent,
  OrgPayload,
} from "@/lib/api-types";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

export function provenanceMode(value: unknown): ProvenanceMode {
  if (value === "live" || value === "simulated" || value === "projected" || value === "no_data") {
    return value;
  }
  return "no_data";
}

export function parseMoney(value: unknown): MoneyPayload | null {
  if (!isRecord(value)) return null;
  const mode = asString(value.mode);
  const provenance = isRecord(value.provenance)
    ? {
        ledger_entries: asNumber(value.provenance.ledger_entries),
        sources: isRecord(value.provenance.sources)
          ? Object.fromEntries(
              Object.entries(value.provenance.sources).filter(
                (entry): entry is [string, number] => typeof entry[1] === "number",
              ),
            )
          : undefined,
        stripe_refs: Array.isArray(value.provenance.stripe_refs)
          ? value.provenance.stripe_refs.map((ref) => (typeof ref === "string" ? ref : null))
          : undefined,
        reason: asString(value.provenance.reason),
      }
    : undefined;
  return {
    earned_minor: value.earned_minor == null ? null : asNumber(value.earned_minor) ?? null,
    gross_received_minor: value.gross_received_minor == null ? null : asNumber(value.gross_received_minor) ?? null,
    minor_unit_exponent: value.minor_unit_exponent == null ? null : asNumber(value.minor_unit_exponent) ?? null,
    currency: asString(value.currency) ?? null,
    mode: mode ?? null,
    charges: Array.isArray(value.charges) ? value.charges : [],
    provenance,
  };
}

export function parseOrg(value: unknown): OrgPayload | null {
  if (!isRecord(value)) return null;
  const agentsRaw = Array.isArray(value.agents) ? value.agents : [];
  const agents: OrgAgent[] = agentsRaw.flatMap((row) => {
    if (!isRecord(row)) return [];
    const id = asString(row.id);
    if (!id) return [];
    const work = isRecord(row.work)
      ? {
          did: asString(row.work.did),
          doing: asString(row.work.doing),
          next: asString(row.work.next),
          live_state: asString(row.work.live_state) ?? null,
        }
      : undefined;
    return [
      {
        id,
        name: asString(row.name),
        role: asString(row.role),
        title: asString(row.title),
        status: asString(row.status),
        work,
      },
    ];
  });
  const summary = isRecord(value.summary)
    ? {
        hired: asNumber(value.summary.hired),
        working: asNumber(value.summary.working),
        watching: asNumber(value.summary.watching),
        ready: asNumber(value.summary.ready),
        paused: asNumber(value.summary.paused),
      }
    : undefined;
  return { agents, summary, stage: asString(value.stage) };
}

export function parseLiveAgents(value: unknown): AgentLiveItem[] {
  const root = isRecord(value) ? value : null;
  const rows = Array.isArray(root?.items) ? root.items : Array.isArray(value) ? value : [];
  return rows.flatMap((row) => {
    if (!isRecord(row)) return [];
    const agent_id = asString(row.agent_id);
    if (!agent_id) return [];
    return [
      {
        agent_id,
        state: asString(row.state),
        current_task_desc: asString(row.current_task_desc) ?? null,
        last_heartbeat: asString(row.last_heartbeat) ?? null,
        blocked_reason: asString(row.blocked_reason) ?? null,
      },
    ];
  });
}

export function parseDashboard(value: unknown): DashboardPayload | null {
  if (!isRecord(value) || !isRecord(value.venture)) return null;
  return value as DashboardPayload;
}

export function parseChatItems(value: unknown): ChatMessage[] {
  const root = isRecord(value) ? value : null;
  const rows = Array.isArray(root?.items) ? root.items : [];
  return rows.filter((row): row is ChatMessage => {
    if (!isRecord(row)) return false;
    return typeof row.id === "string" && typeof row.role === "string";
  });
}

export function moneyChipMode(money: MoneyPayload | null): ProvenanceMode {
  if (!money || money.mode === "no_data" || money.gross_received_minor == null) return "no_data";
  return provenanceMode(money.mode);
}

export function activityProvenance(text: string, kind?: string | null): ProvenanceMode {
  const blob = `${kind || ""} ${text}`.toLowerCase();
  if (
    blob.includes("simulat") ||
    blob.includes("stub") ||
    blob.includes("dry-run") ||
    blob.includes("dry run") ||
    blob.includes("nothing left the building")
  ) {
    return "simulated";
  }
  return "live";
}

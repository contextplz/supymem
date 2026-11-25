import type { OperationsScope } from "./operationsScope.ts";

export type ContentIntentOwner = { actorId: string; workspaceId: string; ventureId: string };
export function contentIntentOwner(scope: OperationsScope | null): ContentIntentOwner | null {
  return scope?.actorId && scope.workspaceId && scope.ventureId
    ? { actorId: scope.actorId, workspaceId: scope.workspaceId, ventureId: scope.ventureId } : null;
}
export function contentIntentKey(owner: ContentIntentOwner | null): string {
  return owner ? `supymem.content.intent:v2:${JSON.stringify([owner.actorId, owner.workspaceId, owner.ventureId])}` : "";
}
export function saveContentIntent<T>(owner: ContentIntentOwner, request: T): string {
  return JSON.stringify({ version: 2, owner, request });
}
export function readContentIntent<T>(raw: string | null, owner: ContentIntentOwner | null): T | null {
  if (!raw || !owner) return null;
  try {
    const value = JSON.parse(raw);
    if (value?.version !== 2 || contentIntentKey(value.owner) !== contentIntentKey(owner)) return null;
    if (value.request?.venture_id !== owner.ventureId || typeof value.request?.idempotency_key !== "string") return null;
    return value.request as T;
  } catch { return null; }
}

/** Only these server rejections occur before a generation run is enqueued. */
export function contentRejectedBeforeEnqueue(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const response = error as { status?: unknown; detail?: unknown };
  if (response.status !== 409 || !response.detail || typeof response.detail !== "object") return false;
  const code = (response.detail as { code?: unknown }).code;
  return code === "content_quote_changed" || code === "content_insufficient_credits";
}

/** A ticket remains invalid after an account change, even if the same owner returns. */
export function contentRequestFence() {
  let epoch = 0, identity = "";
  return {
    activate(value: string) { epoch += 1; identity = value; },
    invalidate() { epoch += 1; identity = ""; },
    capture() {
      const version = epoch, owner = identity;
      return () => !!owner && version === epoch && owner === identity;
    },
  };
}

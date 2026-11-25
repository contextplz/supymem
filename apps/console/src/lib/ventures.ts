import { api, type ApiResult } from "@/lib/client";

export const VENTURE_NAME_MAX_CHARS = 48;

export async function renameVenture(ventureId: string, name: string): Promise<ApiResult> {
  return api(`/api/ventures/${ventureId}`, {
    method: "PATCH",
    body: JSON.stringify({ name: name.trim() }),
  });
}

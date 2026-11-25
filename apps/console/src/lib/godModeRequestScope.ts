/** Local response custody; this never grants server authorization. */
export type GodModeScope = { venture: string; owner: string | null; epoch: number };
export type GodModeRequest = GodModeScope & { sequence: number };
export type GodModeReceipt<T> = { scope: GodModeScope; data: T };

export function sameGodModeScope(a: GodModeScope | null, b: GodModeScope): boolean {
  return Boolean(a && b.owner && a.owner === b.owner && a.venture === b.venture && a.epoch === b.epoch);
}

export function createGodModeRequestFence() {
  let sequence = 0;
  return {
    begin(scope: GodModeScope): GodModeRequest { return { ...scope, sequence: ++sequence }; },
    invalidate() { ++sequence; },
    current(request: GodModeRequest, scope: GodModeScope): boolean {
      return request.sequence === sequence && sameGodModeScope(request, scope);
    },
  };
}

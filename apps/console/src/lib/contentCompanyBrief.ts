type VentureContext = { name?: string; raw_idea?: string };
type CharterContext = {
  one_liner?: string;
  problem?: string;
  audience?: string;
  mvp_scope?: string[] | string;
};

function strings(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String).map((item) => item.trim()).filter(Boolean);
  const item = String(value || "").trim();
  return item ? [item] : [];
}

/** Prefill a content brief only from founder-confirmed company context. */
export function contentBriefFromCompany(
  template: unknown,
  venture: VentureContext,
  charter: CharterContext,
): Record<string, unknown> {
  const root = template && typeof template === "object"
    ? structuredClone(template as Record<string, unknown>)
    : {};
  const product = root.product && typeof root.product === "object"
    ? root.product as Record<string, unknown>
    : {};
  product.name = String(venture.name || "").trim();
  product.one_liner = String(charter.one_liner || venture.raw_idea || "").trim();
  product.differentiators = strings(charter.mvp_scope);
  root.product = product;

  const icp = root.icp && typeof root.icp === "object"
    ? root.icp as Record<string, unknown>
    : {};
  const existing = Array.isArray(icp.segments) && icp.segments[0] && typeof icp.segments[0] === "object"
    ? structuredClone(icp.segments[0] as Record<string, unknown>)
    : {};
  const audience = String(charter.audience || "").trim();
  existing.name = audience;
  existing.pains = strings(charter.problem);
  icp.segments = [existing];
  root.icp = icp;

  // Product claims need evidence. Company context can prefill positioning, never proof.
  root.claims = [];
  return root;
}

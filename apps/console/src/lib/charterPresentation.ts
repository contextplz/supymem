export type CharterLike = {
  one_liner?: string;
  problem?: string;
  audience?: string;
  mvp_scope?: string[] | string;
  explicit_non_goals?: string[] | string;
  success_metric?: string;
  gtm_order?: string;
};

function present(value: unknown): boolean {
  if (Array.isArray(value)) return value.some((item) => String(item || "").trim().length > 0);
  return typeof value === "string" && value.trim().length > 0;
}

export function hasVisibleCharter(content: CharterLike | null | undefined): boolean {
  if (!content) return false;
  return [
    content.one_liner,
    content.problem,
    content.audience,
    content.mvp_scope,
    content.explicit_non_goals,
    content.success_metric,
    content.gtm_order,
  ].some(present);
}

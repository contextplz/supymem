import { CATALOG } from "./featureCatalog.ts";

/** Closed route template for a failed call: ids and tokens become {id}, query and fragment are dropped.
 *  Only words made of letters and hyphens survive, so no id, email or token can reach telemetry. */
export function apiRouteTemplate(path: string): string | null {
  const route = path.split(/[?#]/, 1)[0].replace(/^\/api(?=\/)/, "");
  if (!route.startsWith("/")) return null;
  const template = route.split("/").map((part, i) => i === 0 || /^[a-z][a-z-]*(?:\.[a-z]+)?$/.test(part) ? part : "{id}").join("/");
  return /^\/[a-z0-9_{}./-]{1,80}$/.test(template) ? template : null;
}

// Same matching as feature_catalog.route_feature: ids as {}, longest prefix first.
const PREFIX = /^\/api(?:\/v1)?(?=\/)/;
/** Catalog feature that owns an API route, or null for operator/protocol routes and unmapped ones. */
export function apiRouteFeature(template: string | null): string | null {
  if (!template) return null;
  const path = template.startsWith("/api/w/") ? "/api/w" : template.replace(PREFIX, "").replace(/\{[^}]*\}/g, "{}");
  const hit = CATALOG.routes.find(([prefix]) => path === prefix || path.startsWith(`${prefix}/`));
  const slug = hit?.[1];
  return slug && slug !== "other" && slug !== "shell" && CATALOG.features.includes(slug) ? slug : null;
}

/** Founder-facing copy. Never show an em dash. */
export function stripEmDash(text: string | null | undefined): string {
  if (!text) return "";
  return text.replace(/\u2014/g, " - ").replace(/\u2013/g, "-").replace(/ {2}- {2}/g, " - ");
}

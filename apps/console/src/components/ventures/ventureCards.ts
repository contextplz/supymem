export const CARD_TYPES = [
  { id: "software-ai", label: "Software & AI" },
  { id: "marketplace", label: "Marketplace" },
  { id: "consumer-brand", label: "Consumer Brand" },
  { id: "service-business", label: "Service Business" },
  { id: "media-community", label: "Media & Community" },
  { id: "hardware", label: "Hardware & Deep Tech" },
] as const;

export const CARD_COLORS = ["blue", "green", "pink", "orange"] as const;

export type CardType = (typeof CARD_TYPES)[number]["id"];
export type CardColor = (typeof CARD_COLORS)[number];

export const CARD_ART = ["3", "4", "5", "6", "7", "8", "9", "10"] as const;

export type CardArt = (typeof CARD_ART)[number];

export type CardChoice = { type: CardType; color: CardColor; art: CardArt };

const COLORS = CARD_COLORS;

function hash(seed: string): number {
  let value = 0;
  for (let index = 0; index < seed.length; index += 1) value = (value * 31 + seed.charCodeAt(index)) >>> 0;
  return value;
}

export function suggestType(id: string, name: string, idea?: string): CardType {
  const text = `${name} ${idea || ""}`.toLowerCase();
  if (/hardware|robot|chip|sensor|device|drone|biotech|deep tech|manufactur/.test(text)) return "hardware";
  if (/marketplace|two-sided|buyers and sellers|storefront|uber|rideshare|delivery/.test(text)) return "marketplace";
  if (/newsletter|podcast|community|media|creator|content brand/.test(text)) return "media-community";
  if (/agency|consult|service business|freelance|studio for|dog walk|pet care/.test(text)) return "service-business";
  if (/\bbrand\b|dtc|consumer|retail|fashion|cpg|food brand/.test(text)) return "consumer-brand";
  if (/software|saas|\bapp\b|\bai\b|api|developer|platform|github/.test(text)) return "software-ai";
  return CARD_TYPES[hash(`${id}:type`) % CARD_TYPES.length].id;
}

export function suggestColor(id: string): CardColor {
  return COLORS[hash(id) % COLORS.length];
}

export function isCardArt(value: string | null | undefined): value is CardArt {
  return CARD_ART.includes(value as CardArt);
}

export function artFor(id: string): CardArt {
  return CARD_ART[hash(`${id}:art`) % CARD_ART.length];
}

function storageKey(id: string) {
  return `supymem.venture-card.${id}`;
}

export function isCardType(value: string | null | undefined): value is CardType {
  return CARD_TYPES.some((item) => item.id === value);
}

export function defaultChoice(id: string, name: string, idea?: string): CardChoice {
  return { type: suggestType(id, name, idea), color: suggestColor(id), art: artFor(id) };
}

export function loadCardChoice(id: string, name: string, idea?: string): CardChoice {
  const fallback = defaultChoice(id, name, idea);
  if (typeof window === "undefined") return fallback;
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey(id)) || "null") as Partial<CardChoice> | null;
    const savedType = saved?.type;
    const type = isCardType(savedType) ? savedType : fallback.type;
    const color = COLORS.includes(saved?.color as CardColor) ? saved?.color as CardColor : fallback.color;
    const savedArt = saved?.art;
    const art = isCardArt(savedArt) ? savedArt : fallback.art;
    return { type, color, art };
  } catch {
    return fallback;
  }
}

export function saveCardChoice(id: string, choice: CardChoice): boolean {
  try {
    localStorage.setItem(storageKey(id), JSON.stringify(choice));
    return true;
  } catch {
    return false;
  }
}

export function choiceFromVenture(
  id: string,
  name: string,
  idea: string | undefined,
  cardType: string | null | undefined,
  cardColor: string | null | undefined,
  cardArt?: string | null,
): CardChoice {
  const stored = loadCardChoice(id, name, idea);
  if (isCardArt(cardArt)) stored.art = cardArt;
  const type = isCardType(cardType) ? cardType : null;
  const color = CARD_COLORS.includes(cardColor as CardColor) ? cardColor as CardColor : null;
  if (type && color) return { type, color, art: stored.art };
  if (type) return { type, color: stored.color, art: stored.art };
  if (color) return { type: stored.type, color, art: stored.art };
  return stored;
}

export function cardImage(choice: CardChoice): string {
  const art = isCardArt(choice?.art) ? choice.art : CARD_ART[0];
  return `/venture-cards/${art}.png`;
}

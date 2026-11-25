export function defaultImportProductId(
  current: string,
  products: Array<{ id?: string }> | null | undefined,
): string {
  if (current && products?.some((product) => product.id === current)) return current;
  return products?.find((product) => Boolean(product.id))?.id || "";
}

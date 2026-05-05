const BRAND_LABEL_FA_MAP: Record<string, string> = {
  Peugeot: "پژو",
  MVM: "ام‌وی‌ام",
  Jac: "جک",
};

export function getBrandLabelFa(labelOrValue: string) {
  return BRAND_LABEL_FA_MAP[labelOrValue] ?? labelOrValue;
}

const collator = new Intl.Collator("zh-CN", { numeric: true });

/** 按文本中的数字自然升序排列；未配置编号的项放在末尾，同编号保持原顺序。 */
export function compareNaturalLabels(left: string | null | undefined, right: string | null | undefined): number {
  if (!left) return right ? 1 : 0;
  if (!right) return -1;
  return collator.compare(left, right);
}

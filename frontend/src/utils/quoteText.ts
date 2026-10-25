/**
 * 引用原文来自 OCR / Markdown，可能夹带 HTML 表格片段。
 * 这里只做展示层转换，不改动已保存的证据原文。
 */
const TAG_PATTERN = /<\/?[a-z][^>]*>/i;
const ENTITY_PATTERN = /&(?:[a-z][a-z0-9]+|#\d+|#x[0-9a-f]+);/i;
const LATEX_PATTERN = /\$[^$\n]+\$|\\\(|\\\[|\\[a-zA-Z]+\s*\{/;
const NON_BREAKING_SPACE = String.fromCharCode(0xa0);

function normalizeLine(line: string): string {
  return line
    .replace(/\s+/g, " ")
    .replace(/\s*\|\s*/g, " | ")
    .replace(/^(\s*\|\s*)+|(\s*\|\s*)+$/g, "")
    .trim();
}

function stripMarkdown(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1$2")
    .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1$2");
}

/** OCR 的 Markdown 里可能夹带 LaTeX 公式；保留文字内容，去掉排版命令。 */
function stripLatex(value: string): string {
  if (!LATEX_PATTERN.test(value)) return value;
  let text = value
    .replace(/\$\$([\s\S]*?)\$\$/g, "$1")
    .replace(/\$([^$\n]+)\$/g, "$1")
    .replace(/\\\(([\s\S]*?)\\\)/g, "$1")
    .replace(/\\\[([\s\S]*?)\\\]/g, "$1");
  let previous = "";
  while (text !== previous) {
    previous = text;
    text = text
      .replace(
        /\\(?:text|textrm|textnormal|textbf|textit|underline|overline|mathrm|mathbf|mathit|operatorname|mbox)\s*\{([^{}]*)\}/g,
        "$1",
      )
      .replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "$1/$2")
      .replace(/\\(?:left|right|big|Big|bigg|Bigg)\s*/g, "")
      .replace(/\\(?:quad|qquad|,|;|!|:| )/g, " ")
      .replace(/\\([%_&#$])/g, "$1")
      .replace(/\\[a-zA-Z]+\*?/g, "")
      .replace(/[{}]/g, "");
  }
  return text;
}

function plainLines(value: string | null | undefined): string[] {
  const source = stripLatex((value ?? "").trim());
  if (!source) return [];
  if (!TAG_PATTERN.test(source) && !ENTITY_PATTERN.test(source)) {
    return stripMarkdown(source).split(/\r?\n/).map(normalizeLine).filter(Boolean);
  }
  const withBreaks = source
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(?:td|th)\s*>/gi, " | ")
    .replace(/<\/(?:tr|p|div|li|h[1-6])\s*>/gi, "\n")
    .replace(/<[^>]*>/g, "");
  const decoded = ENTITY_PATTERN.test(withBreaks)
    ? new DOMParser().parseFromString(withBreaks, "text/html").body.textContent ?? withBreaks
    : withBreaks;
  return stripMarkdown(stripLatex(decoded))
    .split(NON_BREAKING_SPACE)
    .join(" ")
    .split(/\r?\n/)
    .map(normalizeLine)
    .filter(Boolean);
}

/** 单行展示：用于表格单元格、搜索与提示文本。 */
export function quoteInlineText(value: string | null | undefined): string {
  const rows = quoteTableRows(value);
  if (rows && rows.length === 2 && rows[0].length === rows[1].length) {
    return rows[0]
      .map((header, index) => (header ? `${header}：${rows[1][index]}` : rows[1][index]))
      .filter(Boolean)
      .join("；");
  }
  const source = value ?? "";
  const tableLike = /<(?:table|tr|td|th)\b/i.test(source) || /^\s*\|.*\|\s*$/m.test(source);
  return plainLines(source).join(tableLike ? "；" : " ");
}

/** 多行展示：用于抽屉中的完整引用原文。 */
export function quoteBlockText(value: string | null | undefined): string {
  return plainLines(value).join("\n");
}

const CELL_PATTERN = /<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi;

function cellText(html: string): string {
  const withBreaks = html.replace(/<br\s*\/?>/gi, " ").replace(/<[^>]*>/g, "");
  const decoded = ENTITY_PATTERN.test(withBreaks)
    ? new DOMParser().parseFromString(withBreaks, "text/html").body.textContent ?? withBreaks
    : withBreaks;
  return stripMarkdown(stripLatex(decoded)).replace(/\s+/g, " ").trim();
}

function htmlTableRows(source: string): string[][] | null {
  if (!/<t[dh]\b/i.test(source)) return null;
  const rows: string[][] = [];
  for (const chunk of source.split(/<\/tr>/i)) {
    const cells = Array.from(chunk.matchAll(CELL_PATTERN), (match) => cellText(match[1]));
    if (cells.length) rows.push(cells);
  }
  return rows.length ? rows : null;
}

function markdownTableRows(source: string): string[][] | null {
  const lines = source
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.includes("|"));
  if (lines.length < 2) return null;
  const rows = lines
    .filter((line) => !/^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$/.test(line))
    .map((line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim()));
  if (!rows.length || rows.some((row) => row.length < 2 || row.length !== rows[0].length)) return null;
  return rows;
}

/** 引用原文里的表格片段：OCR 的 HTML 表格或 Markdown 表格，按行列还原。 */
export function quoteTableRows(value: string | null | undefined): string[][] | null {
  const source = stripLatex((value ?? "").trim());
  if (!source) return null;
  const rows = htmlTableRows(source) ?? markdownTableRows(source);
  if (!rows) return null;
  const cleaned = rows.map((row) => row.map((cell) => cell.trim()));
  return cleaned.some((row) => row.some(Boolean)) ? cleaned : null;
}

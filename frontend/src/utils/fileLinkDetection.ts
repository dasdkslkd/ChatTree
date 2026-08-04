export const FILE_LINK_PREFIX = '#chattree-file-';

export interface LinkRange {
  start: number;
  end: number;
  kind: 'url' | 'file';
  value: string;
}

// 路径段字符集：排除空白、引号、括号、冒号、CJK 与全角标点，
// 避免把括号备注、相邻中文误并入路径
const PATH_SEGMENT = String.raw`[^\s<>"'|?*:()\[\]{}\u3000-\u303f\u3400-\u9fff\uff00-\uffef]`;

// 匹配文件路径：
// - Windows 绝对路径：盘符 + 至少两级
// - POSIX 绝对路径：根 + 至少两级（要求扩展名）
// - 相对路径：仅 ASCII 词字符、至少两级（要求扩展名），可带 ./ 或 ../ 前缀
export const FILE_PATH_PATTERN = new RegExp(
  [
    String.raw`[A-Za-z]:[\\/]${PATH_SEGMENT}+(?:[\\/]${PATH_SEGMENT}+)+`,
    `/(?:${PATH_SEGMENT}+/)+${PATH_SEGMENT}+`,
    String.raw`(?:\.\.?/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+`,
  ].join('|'),
  'g',
);

// 网页链接：仅识别 http(s) 协议；字符集排除 CJK 与全角标点，防止吞并紧随的中文
const URL_PATTERN = /https?:\/\/[^\s<>"'\u3000-\u303f\u3400-\u9fff\uff00-\uffef]+/g;

// 扩展名必须以字母开头：排除 3.10/3.11 这类数字点号文本
const HAS_EXTENSION = /\.[A-Za-z][A-Za-z0-9]*$/;
const WINDOWS_ABSOLUTE = /^[A-Za-z]:[\\/]/;
export const TRAILING_PUNCTUATION = /[.,;:!?，。；：、」』）)\]>]+$/;

function trimTrailingPunctuation(value: string): string {
  let result = value;
  while (TRAILING_PUNCTUATION.test(result)) {
    result = result.replace(TRAILING_PUNCTUATION, '');
  }
  return result;
}

// URL 尾部裁剪：剥离句末标点；括号保持平衡（Wikipedia 式 URL 保留成对括号）
function trimUrlEnd(url: string): string {
  let result = url;
  for (;;) {
    const stripped = result.replace(/[.,;:!?，。；：、」』\]>]+$/, '');
    if (stripped.length < result.length) {
      result = stripped;
      continue;
    }
    const last = result[result.length - 1];
    if (last !== ')' && last !== '）') break;
    const closes = [...result].filter((ch) => ch === ')' || ch === '）').length;
    const opens = [...result].filter((ch) => ch === '(' || ch === '（').length;
    if (closes <= opens) break;
    result = result.slice(0, -1);
  }
  return result;
}

export function findLinkRanges(text: string): LinkRange[] {
  const results: LinkRange[] = [];
  const urlRanges: LinkRange[] = [];

  for (const match of text.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    const value = trimUrlEnd(match[0]);
    const end = start + value.length;
    if (end > start) {
      const range = { start, end, kind: 'url' as const, value };
      results.push(range);
      urlRanges.push(range);
    }
  }

  for (const match of text.matchAll(FILE_PATH_PATTERN)) {
    const start = match.index ?? 0;
    const raw = match[0];
    // URL 优先：跳过与网页链接重叠的匹配（避免把 URL 片段当作本地路径）
    if (urlRanges.some((range) => start < range.end && start + raw.length > range.start)) continue;
    // 非 Windows 路径要求最后一段带字母开头的扩展名
    if (!WINDOWS_ABSOLUTE.test(raw)) {
      const segments = raw.split(/[\\/]+/);
      if (!HAS_EXTENSION.test(segments[segments.length - 1])) continue;
    }
    const value = trimTrailingPunctuation(raw);
    const end = start + value.length;
    if (end > start) results.push({ start, end, kind: 'file', value });
  }

  return results.sort((a, b) => a.start - b.start);
}

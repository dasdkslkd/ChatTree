export const FILE_LINK_PREFIX = '#chattree-file-';

// 匹配文件路径：
// - Windows 绝对路径：盘符 + 至少两级目录
// - POSIX 绝对路径：根 + 至少一级目录 + 带扩展名的文件名
// - 相对路径：至少一级目录 + 带扩展名的文件名
export const FILE_PATH_PATTERN =
  /[A-Za-z]:[\\/][^\s<>"|?*]+(?:[\\/][^\s<>"|?*]+)+|\/(?:[^\s/]+\/)+[^\s/]+\.[^\s/]+|(?:\.\.?\/)*[^\s/]+(?:\/[^\s/]+)+\.[^\s/]+/g;
export const TRAILING_PUNCTUATION = /[.,;:!?，。；：、」』）)\]>]+$/;

export function findFilePathRanges(text: string): Array<{ start: number; end: number; path: string }> {
  const results: Array<{ start: number; end: number; path: string }> = [];
  for (const match of text.matchAll(FILE_PATH_PATTERN)) {
    const start = match.index ?? 0;
    const path = match[0].replace(TRAILING_PUNCTUATION, '');
    const end = start + path.length;
    if (end > start) results.push({ start, end, path });
  }
  return results;
}

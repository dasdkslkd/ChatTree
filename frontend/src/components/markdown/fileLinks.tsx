import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from 'react';
import type { Components } from 'react-markdown';
import { toast } from 'sonner';
import { getApiErrorMessage } from '../../api/errors';
import { filesApi } from '../../api/files';

export const FILE_LINK_SCHEME = 'chattree-file://';

// 匹配绝对路径：Windows（盘符 + 至少两级目录）或 POSIX（根 + 至少一级目录）
const FILE_PATH_PATTERN =
  /[A-Za-z]:[\\/][^\s<>"|?*]+(?:[\\/][^\s<>"|?*]+)+|\/(?:[^\s/]+\/)+[^\s/]+/g;
const TRAILING_PUNCTUATION = /[.,;:!?，。；：、」』）)\]>]+$/;

export function findFilePathRanges(text: string): Array<{ start: number; end: number }> {
  const ranges: Array<{ start: number; end: number }> = [];
  for (const match of text.matchAll(FILE_PATH_PATTERN)) {
    const start = match.index ?? 0;
    const end = start + match[0].replace(TRAILING_PUNCTUATION, '').length;
    if (end > start) ranges.push({ start, end });
  }
  return ranges;
}

function FilePathLink({ path }: { path: string }) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    filesApi.open(path).catch((error) => {
      toast.error(getApiErrorMessage(error, '打开文件失败'));
    });
  };
  return (
    <a href={`${FILE_LINK_SCHEME}${encodeURIComponent(path)}`} onClick={handleClick}>
      {path}
    </a>
  );
}

export function FileLinkText({ children }: { children: string }) {
  const text = String(children);
  const ranges = findFilePathRanges(text);
  if (ranges.length === 0) return text;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const { start, end } of ranges) {
    if (start > cursor) nodes.push(text.slice(cursor, start));
    nodes.push(<FilePathLink key={`${start}-${end}`} path={text.slice(start, end)} />);
    cursor = end;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

export function FileOpenLink({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (href?.startsWith(FILE_LINK_SCHEME)) {
    const path = decodeURIComponent(href.slice(FILE_LINK_SCHEME.length));
    return <FilePathLink path={path} />;
  }
  return (
    <a href={href} {...rest}>
      {children}
    </a>
  );
}

export const fileLinkComponents: Components = {
  a: FileOpenLink,
  text: FileLinkText as unknown as Components['text'],
};

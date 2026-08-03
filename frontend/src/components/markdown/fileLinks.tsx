import type { MouseEvent, ReactNode } from 'react';
import type { Components } from 'react-markdown';
import { toast } from 'sonner';
import { getApiErrorMessage } from '../../api/errors';
import { filesApi } from '../../api/files';
import { FILE_LINK_PREFIX, findFilePathRanges } from '../../utils/fileLinkDetection';

export function FilePathLink({ path }: { path: string }) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    filesApi.open(path).catch((error) => {
      toast.error(getApiErrorMessage(error, '打开文件失败'));
    });
  };
  return (
    <a href={`${FILE_LINK_PREFIX}${encodeURIComponent(path)}`} onClick={handleClick}>
      {path}
    </a>
  );
}

export function renderFileLinkNodes(text: string): ReactNode {
  const ranges = findFilePathRanges(text);
  if (ranges.length === 0) return text;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const { start, end, path } of ranges) {
    if (start > cursor) nodes.push(text.slice(cursor, start));
    nodes.push(<FilePathLink key={`${start}-${end}`} path={path} />);
    cursor = end;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function walkChildrenForFileLinks(node: ReactNode): ReactNode {
  if (typeof node === 'string') return renderFileLinkNodes(node);
  if (node == null || typeof node !== 'object') return node;
  if (Array.isArray(node)) return node.map(walkChildrenForFileLinks);
  if ('props' in node && (node as { type?: unknown }).type !== 'a') {
    const props = node.props as { children?: ReactNode };
    if (props.children != null) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return { ...node as any, props: { ...props, children: walkChildrenForFileLinks(props.children) } };
    }
  }
  return node;
}

export function FileLinkWrapper({ children }: { children?: ReactNode }) {
  return walkChildrenForFileLinks(children);
}

export function FileOpenLink({
  href,
  children,
  ...rest
}: {
  href?: string;
  children?: ReactNode;
} & React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (href?.startsWith(FILE_LINK_PREFIX)) {
    const path = decodeURIComponent(href.slice(FILE_LINK_PREFIX.length));
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
  p: FileLinkWrapper as unknown as Components['p'],
  h1: FileLinkWrapper as unknown as Components['h1'],
  h2: FileLinkWrapper as unknown as Components['h2'],
  h3: FileLinkWrapper as unknown as Components['h3'],
  h4: FileLinkWrapper as unknown as Components['h4'],
  h5: FileLinkWrapper as unknown as Components['h5'],
  h6: FileLinkWrapper as unknown as Components['h6'],
  li: FileLinkWrapper as unknown as Components['li'],
  td: FileLinkWrapper as unknown as Components['td'],
  th: FileLinkWrapper as unknown as Components['th'],
  blockquote: FileLinkWrapper as unknown as Components['blockquote'],
};

import type { MouseEvent, ReactNode } from 'react';
import type { Components } from 'react-markdown';
import { toast } from 'sonner';
import { getApiErrorMessage } from '../../api/errors';
import { filesApi } from '../../api/files';
import { useConversationStore } from '../../store/conversationStore';
import { FILE_LINK_PREFIX, findLinkRanges } from '../../utils/fileLinkDetection';

// 本文件同时导出链接组件与供 MarkdownContent/测试复用的组件映射表，属合理结构
/* eslint-disable react-refresh/only-export-components */

const ABSOLUTE_PATH = /^[A-Za-z]:[\\/]|^[/~]/;
// URL 中不允许出现的字符：GFM autolink 会把紧随的中文吞进链接，在此截断修正
const URL_UNSAFE = /[\s<>"\u3000-\u303f\u3400-\u9fff\uff00-\uffef]/;

export function FilePathLink({ path }: { path: string }) {
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    // 相对路径按当前会话的 workspace 根目录解析；绝对路径原样提交
    const workspace = useConversationStore.getState().currentConversation?.workspace;
    const root = workspace?.workspace_roots?.[0] || workspace?.cwd;
    const absolute = !root || ABSOLUTE_PATH.test(path)
      ? path
      : `${root.replace(/[\\/]+$/, '')}/${path.replace(/^[\\/]+/, '')}`;
    filesApi.open(absolute).catch((error) => {
      toast.error(getApiErrorMessage(error, '打开文件失败'));
    });
  };
  return (
    <a href={`${FILE_LINK_PREFIX}${encodeURIComponent(path)}`} onClick={handleClick}>
      {path}
    </a>
  );
}

function renderLinkNodes(text: string): ReactNode {
  const ranges = findLinkRanges(text);
  if (ranges.length === 0) return text;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start > cursor) nodes.push(text.slice(cursor, range.start));
    nodes.push(
      range.kind === 'url' ? (
        <a
          key={`${range.start}-${range.end}`}
          href={range.value}
          target="_blank"
          rel="noopener noreferrer"
        >
          {range.value}
        </a>
      ) : (
        <FilePathLink key={`${range.start}-${range.end}`} path={range.value} />
      ),
    );
    cursor = range.end;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}

function walkChildrenForFileLinks(node: ReactNode): ReactNode {
  if (typeof node === 'string') return renderLinkNodes(node);
  if (node == null || typeof node !== 'object') return node;
  if (Array.isArray(node)) return node.map(walkChildrenForFileLinks);
  if ('props' in node) {
    const type = (node as { type?: unknown }).type;
    // 已有链接不再二次处理；行内代码与代码块内同样识别链接
    if (type === 'a') return node;
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
  if (href?.startsWith('file://')) {
    // file:///D:/a/b.txt → D:/a/b.txt；file:///home/user/x → /home/user/x
    const raw = decodeURIComponent(href.slice('file://'.length));
    const path = /^\/[A-Za-z]:/.test(raw) ? raw.slice(1) : raw;
    return <FilePathLink path={path} />;
  }
  if (href && /^https?:\/\//.test(href)) {
    const cut = href.search(URL_UNSAFE);
    if (cut > 0) {
      const url = href.slice(0, cut);
      return [
        <a key="link" href={url} target="_blank" rel="noopener noreferrer">
          {url}
        </a>,
        href.slice(cut),
      ];
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
        {children}
      </a>
    );
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
  pre: FileLinkWrapper as unknown as Components['pre'],
};

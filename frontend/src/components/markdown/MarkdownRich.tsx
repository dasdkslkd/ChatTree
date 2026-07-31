import ReactMarkdown, { type Options } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import rehypeKatex from 'rehype-katex';
import { rehypeMermaid } from 'react-markdown-mermaid';
import 'katex/dist/katex.min.css';
import { normalizeMathDelimiters } from '../../utils/normalizeMathDelimiters';
import type { MarkdownRendererProps } from './MarkdownBasic';

const katexOptions = { throwOnError: false, trust: false };

export default function MarkdownRich({ children, components, enableMermaid = false }: MarkdownRendererProps & { enableMermaid?: boolean }) {
  const rehypePlugins: NonNullable<Options['rehypePlugins']> = [
    rehypeRaw,
    rehypeSanitize,
    [rehypeKatex, katexOptions],
  ];
  if (enableMermaid) rehypePlugins.push(rehypeMermaid as never);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={rehypePlugins}
      components={components}
    >
      {normalizeMathDelimiters(children)}
    </ReactMarkdown>
  );
}

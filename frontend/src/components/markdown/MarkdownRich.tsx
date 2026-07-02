import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize from 'rehype-sanitize';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import { normalizeMathDelimiters } from '../../utils/normalizeMathDelimiters';
import type { MarkdownRendererProps } from './MarkdownBasic';

const katexOptions = {
  throwOnError: false,
  trust: false,
};

export default function MarkdownRich({ children, components }: MarkdownRendererProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, rehypeSanitize, [rehypeKatex, katexOptions]]}
      components={components}
    >
      {normalizeMathDelimiters(children)}
    </ReactMarkdown>
  );
}

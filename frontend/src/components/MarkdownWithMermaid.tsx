import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { rehypeMermaid } from 'react-markdown-mermaid';

interface MarkdownWithMermaidProps {
  children: string;
  components?: Components;
}

export default function MarkdownWithMermaid({ children, components }: MarkdownWithMermaidProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeMermaid as any]}
      components={components}
    >
      {children}
    </ReactMarkdown>
  );
}

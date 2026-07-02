import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

export interface MarkdownRendererProps {
  children: string;
  components?: Components;
}

export default function MarkdownBasic({ children, components }: MarkdownRendererProps) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {children}
    </ReactMarkdown>
  );
}

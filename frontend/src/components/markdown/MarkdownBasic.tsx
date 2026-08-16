import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

export interface MarkdownRendererProps {
  children: string;
  components?: Components;
}

const remarkPlugins = [remarkGfm];

export default function MarkdownBasic({ children, components }: MarkdownRendererProps) {
  return (
    <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
      {children}
    </ReactMarkdown>
  );
}
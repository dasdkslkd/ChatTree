import { lazy, Suspense } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

const MarkdownWithMermaid = lazy(() => import('./MarkdownWithMermaid'));

interface MarkdownContentProps {
  children: string;
  components?: Components;
  enableMermaid?: boolean;
}

const mermaidFencePattern = /(^|\n)(```|~~~)\s*mermaid\b/i;

export default function MarkdownContent({ children, components, enableMermaid = false }: MarkdownContentProps) {
  const shouldLoadMermaid = enableMermaid && mermaidFencePattern.test(children);

  if (shouldLoadMermaid) {
    return (
      <Suspense fallback={<ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{children}</ReactMarkdown>}>
        <MarkdownWithMermaid components={components}>{children}</MarkdownWithMermaid>
      </Suspense>
    );
  }

  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {children}
    </ReactMarkdown>
  );
}

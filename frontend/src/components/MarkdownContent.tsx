import { lazy, Suspense } from 'react';
import { type Components } from 'react-markdown';
import MarkdownBasic from './markdown/MarkdownBasic';
import { detectMarkdownFeatures } from '../utils/markdownFeatures';

const MarkdownRich = lazy(() => import('./markdown/MarkdownRich'));
const MarkdownWithMermaid = lazy(() => import('./markdown/MarkdownWithMermaid'));

interface MarkdownContentProps {
  children: string;
  components?: Components;
  enableMermaid?: boolean;
}

export default function MarkdownContent({ children, components, enableMermaid = false }: MarkdownContentProps) {
  const features = detectMarkdownFeatures(children);
  const shouldLoadMermaid = enableMermaid && features.hasMermaid;

  if (shouldLoadMermaid) {
    return (
      <Suspense fallback={<MarkdownBasic components={components}>{children}</MarkdownBasic>}>
        <MarkdownWithMermaid components={components}>{children}</MarkdownWithMermaid>
      </Suspense>
    );
  }

  if (features.hasMath || features.hasRawHtml) {
    return (
      <Suspense fallback={<MarkdownBasic components={components}>{children}</MarkdownBasic>}>
        <MarkdownRich components={components}>{children}</MarkdownRich>
      </Suspense>
    );
  }

  return (
    <MarkdownBasic components={components}>{children}</MarkdownBasic>
  );
}

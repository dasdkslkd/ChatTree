import { lazy, Suspense } from 'react';
import { type Components } from 'react-markdown';
import MarkdownBasic from './markdown/MarkdownBasic';
import { detectMarkdownFeatures } from '../utils/markdownFeatures';

const MarkdownRich = lazy(() => import('./markdown/MarkdownRich'));

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
        <MarkdownRich components={components} enableMermaid>{children}</MarkdownRich>
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

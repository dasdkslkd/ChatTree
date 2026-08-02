import { lazy, Suspense } from 'react';
import { type Components } from 'react-markdown';
import MarkdownBasic from './markdown/MarkdownBasic';
import { fileLinkComponents } from './markdown/fileLinks';
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
  const mergedComponents: Components = components
    ? { ...fileLinkComponents, ...components }
    : fileLinkComponents;

  if (shouldLoadMermaid) {
    return (
      <Suspense fallback={<MarkdownBasic components={mergedComponents}>{children}</MarkdownBasic>}>
        <MarkdownRich components={mergedComponents} enableMermaid>{children}</MarkdownRich>
      </Suspense>
    );
  }

  if (features.hasMath || features.hasRawHtml) {
    return (
      <Suspense fallback={<MarkdownBasic components={mergedComponents}>{children}</MarkdownBasic>}>
        <MarkdownRich components={mergedComponents}>{children}</MarkdownRich>
      </Suspense>
    );
  }

  return (
    <MarkdownBasic components={mergedComponents}>{children}</MarkdownBasic>
  );
}

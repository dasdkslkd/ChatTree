declare module 'react-syntax-highlighter' {
  import type { ComponentType, CSSProperties } from 'react';

  export type SyntaxHighlighterProps = {
    children?: string;
    language?: string;
    style?: Record<string, CSSProperties>;
    customStyle?: CSSProperties;
    codeTagProps?: { style?: CSSProperties };
    wrapLongLines?: boolean;
    showLineNumbers?: boolean;
  };

  const SyntaxHighlighter: ComponentType<SyntaxHighlighterProps>;
  export const Prism: ComponentType<SyntaxHighlighterProps>;
  export default SyntaxHighlighter;
}

declare module 'react-syntax-highlighter/dist/esm/prism-light' {
  import type { ComponentType, CSSProperties } from 'react';

  export type SyntaxHighlighterProps = {
    children?: string;
    language?: string;
    style?: Record<string, CSSProperties>;
    customStyle?: CSSProperties;
    codeTagProps?: { style?: CSSProperties };
    wrapLongLines?: boolean;
    showLineNumbers?: boolean;
  };

  type SyntaxHighlighterComponent = ComponentType<SyntaxHighlighterProps> & {
    registerLanguage: (name: string, language: unknown) => void;
  };

  const SyntaxHighlighter: SyntaxHighlighterComponent;
  export default SyntaxHighlighter;
}

declare module 'react-syntax-highlighter/dist/esm/styles/prism/one-dark' {
  import type { CSSProperties } from 'react';

  const oneDark: Record<string, CSSProperties>;
  export default oneDark;
}

declare module 'react-syntax-highlighter/dist/esm/languages/prism/*' {
  const language: unknown;
  export default language;
}

import {
  lazy,
  Suspense,
  useState,
  useRef,
  isValidElement,
  type ReactNode,
  type HTMLAttributes,
} from 'react';
import { Check, Copy, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { SyntaxHighlighter, oneDark } from './languages';

const MarkdownContent = lazy(() => import('../MarkdownContent'));

export function normalizeCodeLanguage(language: string): string {
  const normalized = language.toLowerCase();
  if (normalized === 'js') return 'javascript';
  if (normalized === 'ts') return 'typescript';
  if (normalized === 'py') return 'python';
  if (normalized === 'ps1' || normalized === 'pwsh' || normalized === 'shell') return 'powershell';
  if (normalized === 'sh' || normalized === 'zsh') return 'bash';
  if (normalized === 'yml') return 'yaml';
  return normalized;
}

export function getCodeBlockPayload(children: ReactNode): { code: string; language: string | null } | null {
  const codeElement = Array.isArray(children)
    ? children.find((child) => isValidElement(child))
    : children;
  if (!isValidElement(codeElement)) return null;
  const props = codeElement.props as { className?: string; children?: ReactNode };
  const className = props.className || '';
  const languageMatch = className.match(/language-([\w-]+)/);
  const rawChildren = props.children;
  const code = Array.isArray(rawChildren)
    ? rawChildren.map((child) => String(child)).join('')
    : typeof rawChildren === 'string'
      ? rawChildren
      : rawChildren == null
        ? ''
        : String(rawChildren);
  return {
    code: code.replace(/\n$/, ''),
    language: languageMatch ? normalizeCodeLanguage(languageMatch[1]) : null,
  };
}

export function CodeBlockWrapper({ children, ...props }: HTMLAttributes<HTMLPreElement>) {
  const [copied, setCopied] = useState(false);
  const codeRef = useRef<HTMLDivElement>(null);
  const payload = getCodeBlockPayload(children);
  const languageLabel = payload?.language || '代码';

  const handleCopy = () => {
    const pre = codeRef.current?.querySelector('pre');
    const text = pre?.textContent || '';
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div ref={codeRef} className="code-block-wrapper my-2">
      <div className="code-toolbar-wrapper">
        <div className="code-toolbar">
          <span className="text-xs text-muted-foreground select-none">{languageLabel}</span>
          <button
            className="flex items-center gap-1 px-0 py-1.5 rounded text-xs text-muted-foreground hover:text-foreground hover:bg-black/5 transition-colors cursor-pointer"
            onClick={handleCopy}
            aria-label="复制代码"
          >
            {copied ? (
              <><Check className="h-3 w-3" /> 已复制</>
            ) : (
              <><Copy className="h-3 w-3" /> 复制</>
            )}
          </button>
        </div>
      </div>
      {payload?.language ? (
        <SyntaxHighlighter
          language={payload.language}
          style={oneDark}
          customStyle={{
            margin: 0,
            padding: '10px 12px',
            background: 'transparent',
            fontSize: 13,
            lineHeight: '20px',
          }}
          codeTagProps={{
            style: {
              fontFamily: 'var(--font-mono, "JetBrains Mono", ui-monospace, monospace)',
            },
          }}
        >
          {payload.code}
        </SyntaxHighlighter>
      ) : (
        <pre {...props}>
          {children}
        </pre>
      )}
    </div>
  );
}

export const markdownComponents = {
  pre: CodeBlockWrapper,
};

export function MarkdownFallback({ content }: { content: string }) {
  return <span className="whitespace-pre-wrap break-words">{content}</span>;
}

export function MarkdownView({ content, enableMermaid = false }: { content: string; enableMermaid?: boolean }) {
  return (
    <Suspense fallback={<MarkdownFallback content={content} />}>
      <MarkdownContent components={markdownComponents} enableMermaid={enableMermaid}>
        {content}
      </MarkdownContent>
    </Suspense>
  );
}

export function ThinkingBlock({ reasoning, streaming }: { reasoning: string; streaming?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!reasoning) return null;
  const label = streaming ? '思考中' : '思考完成';
  return (
    <div className={cn('thought', expanded && 'expanded')}>
      <button
        type="button"
        className="thought-head"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        <ChevronRight className="thought-chevron" />
        <span>{label}</span>
      </button>
      <div className="thought-body-shell" aria-hidden={!expanded}>
        <div className="thought-body-clip">
          <div className="thought-body custom-scrollbar">
            {reasoning}
          </div>
        </div>
      </div>
    </div>
  );
}

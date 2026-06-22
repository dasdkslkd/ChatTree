import {
  lazy,
  Suspense,
  useEffect,
  useState,
  useRef,
  useLayoutEffect,
  useCallback,
  useMemo,
  type DragEvent,
} from 'react';
import SyntaxHighlighter from 'react-syntax-highlighter/dist/esm/prism-light';
import oneDark from 'react-syntax-highlighter/dist/esm/styles/prism/one-dark';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import c from 'react-syntax-highlighter/dist/esm/languages/prism/c';
import cpp from 'react-syntax-highlighter/dist/esm/languages/prism/cpp';
import csharp from 'react-syntax-highlighter/dist/esm/languages/prism/csharp';
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css';
import diff from 'react-syntax-highlighter/dist/esm/languages/prism/diff';
import docker from 'react-syntax-highlighter/dist/esm/languages/prism/docker';
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go';
import ini from 'react-syntax-highlighter/dist/esm/languages/prism/ini';
import java from 'react-syntax-highlighter/dist/esm/languages/prism/java';
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx';
import less from 'react-syntax-highlighter/dist/esm/languages/prism/less';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup';
import php from 'react-syntax-highlighter/dist/esm/languages/prism/php';
import powershell from 'react-syntax-highlighter/dist/esm/languages/prism/powershell';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import ruby from 'react-syntax-highlighter/dist/esm/languages/prism/ruby';
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust';
import scss from 'react-syntax-highlighter/dist/esm/languages/prism/scss';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import {
  Plus, X, MoreHorizontal, ChevronRight,
  Copy, Check, Pencil, Loader2, RotateCcw, Network, MessageSquare, Trash2, FileText, Download, FolderOpen, FolderPlus, Search, Settings,
  PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Archive,
} from 'lucide-react';
import { conversationApi } from '../api/conversation';
import { messageApi, type ToolResultSlice } from '../api/message';
import type { SendMessageRequest, ToolApprovalDecision, ToolApprovalPayload, ToolApprovalScope, ToolPermissionMode } from '../types/message';
import type { WorkspaceContext } from '../types/conversation';
import { useConversationStore } from '../store/conversationStore';
import { useModelStore } from '../store/modelStore';
import { useNavigationStore } from '../store/navigationStore';
import { useStreamingManager } from '../hooks/useStreamingManager';
import { streamManager } from '../services/streamManager';
import { getGenerationStatusText, getStreamStatusText as getStreamStatusLabel } from '../utils/generationStatus';
import {
  extractToolResultEnvelope,
  formatToolArguments,
  formatToolOutput,
  isToolResultError,
  summarizeToolCall,
  type ToolResultEnvelope,
} from '../utils/toolDisplay';
import { ChatInput } from '../components/ChatInput';
import TreeView from './TreeView';
import {
  getVisibleProjectConversations,
  getWorkspaceForNewConversation,
  groupConversationsByProject,
  encodeProjectId,
} from '../utils/projectGroups';

const MarkdownContent = lazy(() => import('../components/MarkdownContent'));
const MANUAL_PROJECTS_STORAGE_KEY = 'chattree.manualProjectWorkspaces';
const PROJECT_ORDER_STORAGE_KEY = 'chattree.projectOrder';

SyntaxHighlighter.registerLanguage('bash', bash);
SyntaxHighlighter.registerLanguage('batch', bash);
SyntaxHighlighter.registerLanguage('c', c);
SyntaxHighlighter.registerLanguage('cpp', cpp);
SyntaxHighlighter.registerLanguage('csharp', csharp);
SyntaxHighlighter.registerLanguage('css', css);
SyntaxHighlighter.registerLanguage('diff', diff);
SyntaxHighlighter.registerLanguage('docker', docker);
SyntaxHighlighter.registerLanguage('go', go);
SyntaxHighlighter.registerLanguage('ini', ini);
SyntaxHighlighter.registerLanguage('java', java);
SyntaxHighlighter.registerLanguage('javascript', javascript);
SyntaxHighlighter.registerLanguage('json', json);
SyntaxHighlighter.registerLanguage('jsx', jsx);
SyntaxHighlighter.registerLanguage('less', less);
SyntaxHighlighter.registerLanguage('markdown', markdown);
SyntaxHighlighter.registerLanguage('markup', markup);
SyntaxHighlighter.registerLanguage('html', markup);
SyntaxHighlighter.registerLanguage('xml', markup);
SyntaxHighlighter.registerLanguage('php', php);
SyntaxHighlighter.registerLanguage('powershell', powershell);
SyntaxHighlighter.registerLanguage('python', python);
SyntaxHighlighter.registerLanguage('ruby', ruby);
SyntaxHighlighter.registerLanguage('rust', rust);
SyntaxHighlighter.registerLanguage('scss', scss);
SyntaxHighlighter.registerLanguage('sql', sql);
SyntaxHighlighter.registerLanguage('toml', toml);
SyntaxHighlighter.registerLanguage('tsx', tsx);
SyntaxHighlighter.registerLanguage('typescript', typescript);
SyntaxHighlighter.registerLanguage('yaml', yaml);

function loadManualProjectWorkspaces(): WorkspaceContext[] {
  try {
    const raw = window.localStorage.getItem(MANUAL_PROJECTS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is WorkspaceContext =>
      !!item && typeof item.cwd === 'string' && Array.isArray(item.workspace_roots)
    );
  } catch {
    return [];
  }
}

function saveManualProjectWorkspaces(workspaces: WorkspaceContext[]) {
  window.localStorage.setItem(MANUAL_PROJECTS_STORAGE_KEY, JSON.stringify(workspaces));
}

function loadProjectOrder(): string[] {
  try {
    const raw = window.localStorage.getItem(PROJECT_ORDER_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
  } catch (_) {
    return [];
  }
}

function saveProjectOrder(order: string[]) {
  window.localStorage.setItem(PROJECT_ORDER_STORAGE_KEY, JSON.stringify(order));
}

function mergeManualProjectWorkspace(workspaces: WorkspaceContext[], workspace: WorkspaceContext): WorkspaceContext[] {
  const existing = workspaces.filter((item) => item.cwd !== workspace.cwd);
  return [workspace, ...existing];
}

function formatConversationTime(timestamp: number | undefined): string {
  if (!timestamp) return '';
  const timeMs = timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
  const diffMinutes = Math.max(0, Math.floor((Date.now() - timeMs) / 60000));
  if (diffMinutes < 1) return '刚刚';
  if (diffMinutes < 60) return `${diffMinutes} 分`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} 小时`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays} 天`;
}

/* ---------- Markdown custom code blocks ---------- */

function CodeBlockWrapper({ children, ...props }: React.HTMLAttributes<HTMLPreElement>) {
  const [copied, setCopied] = useState(false);
  const codeRef = useRef<HTMLDivElement>(null);

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
          <span className="text-xs text-muted-foreground select-none">代码</span>
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
      <pre {...props}>
        {children}
      </pre>
    </div>
  );
}

const markdownComponents = {
  pre: CodeBlockWrapper,
};

function MarkdownFallback({ content }: { content: string }) {
  return <span className="whitespace-pre-wrap break-words">{content}</span>;
}

function MarkdownView({ content, enableMermaid = false }: { content: string; enableMermaid?: boolean }) {
  return (
    <Suspense fallback={<MarkdownFallback content={content} />}>
      <MarkdownContent components={markdownComponents} enableMermaid={enableMermaid}>
        {content}
      </MarkdownContent>
    </Suspense>
  );
}

const languageByExtension: Record<string, string> = {
  bash: 'bash',
  bat: 'batch',
  c: 'c',
  cmd: 'batch',
  conf: 'ini',
  cpp: 'cpp',
  cs: 'csharp',
  css: 'css',
  csv: 'csv',
  dockerfile: 'docker',
  env: 'ini',
  ex: 'elixir',
  exs: 'elixir',
  fish: 'fish',
  go: 'go',
  h: 'c',
  hpp: 'cpp',
  html: 'html',
  htm: 'html',
  ini: 'ini',
  java: 'java',
  js: 'javascript',
  json: 'json',
  jsx: 'jsx',
  kt: 'kotlin',
  less: 'less',
  log: 'text',
  lua: 'lua',
  md: 'markdown',
  perl: 'perl',
  php: 'php',
  pl: 'perl',
  properties: 'properties',
  ps1: 'powershell',
  py: 'python',
  r: 'r',
  rb: 'ruby',
  rs: 'rust',
  sass: 'sass',
  scala: 'scala',
  scss: 'scss',
  sh: 'bash',
  sql: 'sql',
  svelte: 'svelte',
  swift: 'swift',
  toml: 'toml',
  ts: 'typescript',
  tsx: 'tsx',
  txt: 'text',
  vue: 'vue',
  xml: 'xml',
  yaml: 'yaml',
  yml: 'yaml',
  zsh: 'bash',
};

function inferPreviewLanguage(filename: string) {
  const normalized = filename.toLowerCase();
  const base = normalized.split(/[\\/]/).pop() || normalized;
  if (base === 'dockerfile') return 'docker';
  if (base === 'makefile') return 'makefile';
  if (base === '.gitignore') return 'gitignore';
  const ext = base.includes('.') ? base.split('.').pop() || '' : '';
  return languageByExtension[ext] || 'text';
}

function FilePreviewCode({ name, content }: { name: string; content: string }) {
  const language = inferPreviewLanguage(name);

  return (
    <div className="file-preview-code-shell custom-scrollbar">
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{
          margin: 0,
          minHeight: '100%',
          background: 'transparent',
          padding: '14px 16px',
        }}
        codeTagProps={{
          style: {
            fontFamily: 'var(--font-mono)',
            fontSize: '13px',
            lineHeight: '20px',
          },
        }}
        wrapLongLines={false}
        showLineNumbers={content.includes('\n') && content.split('\n').length > 12}
      >
        {content}
      </SyntaxHighlighter>
    </div>
  );
}

/* ---------- Collapsible thinking (reasoning) block ---------- */

function ThinkingBlock({ reasoning, streaming }: { reasoning: string; streaming?: boolean }) {
  // 默认折叠；流式进行中也保持折叠（用户可手动展开看实时思考）。
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

type ToolCallLike = {
  id?: string;
  name?: string;
  arguments?: unknown;
  args?: unknown;
  input?: unknown;
  function?: {
    name?: string;
    arguments?: unknown;
  };
};

type ToolMessageLike = {
  name?: string;
  content?: unknown;
  output?: unknown;
  result?: unknown;
  error?: unknown;
  tool_call_id?: string;
};

type ToolRenderItem = {
  key: string;
  name: string;
  summary: string;
  argsText: string;
  outputText: string;
  status: 'done' | 'error' | 'running';
  resultEnvelope: ToolResultEnvelope | null;
};

const TOOL_DISPLAY_LIMIT = 100;

function limitToolDisplayText(text: string): string {
  if (text.length <= TOOL_DISPLAY_LIMIT) return text;
  return `${text.slice(0, TOOL_DISPLAY_LIMIT - 3)}...`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function getToolName(toolCall: ToolCallLike | null, toolMessage?: ToolMessageLike | null): string {
  return toolCall?.function?.name || toolCall?.name || toolMessage?.name || 'tool';
}

function getToolRawArgs(toolCall: ToolCallLike | null): unknown {
  return toolCall?.function?.arguments ?? toolCall?.arguments ?? toolCall?.args ?? toolCall?.input;
}

function getToolArgs(toolCall: ToolCallLike | null): string {
  if (!toolCall) return '';
  return formatToolArguments(getToolRawArgs(toolCall));
}

function getToolOutput(toolMessage?: ToolMessageLike | null): string {
  return formatToolOutput(toolMessage);
}

function isToolError(toolMessage?: ToolMessageLike | null): boolean {
  return isToolResultError(toolMessage);
}

function makeToolItem(
  toolCall: ToolCallLike | null,
  toolMessage: ToolMessageLike | null,
  fallbackKey: string,
): ToolRenderItem {
  const name = getToolName(toolCall, toolMessage);
  const rawArgs = getToolRawArgs(toolCall);
  const argsText = getToolArgs(toolCall);
  const outputText = limitToolDisplayText(getToolOutput(toolMessage));
  const summary = limitToolDisplayText(
    toolCall ? summarizeToolCall(name, rawArgs) : outputText || '工具结果',
  );
  const resultEnvelope = extractToolResultEnvelope(toolMessage);
  return {
    key: toolCall?.id || toolMessage?.tool_call_id || fallbackKey,
    name,
    summary,
    argsText,
    outputText,
    status: toolMessage ? (isToolError(toolMessage) ? 'error' : 'done') : 'running',
    resultEnvelope,
  };
}

function findToolMessage(toolMessages: ToolMessageLike[], toolCall: ToolCallLike, index: number): ToolMessageLike | null {
  if (toolCall.id) {
    const matched = toolMessages.find((message) => message.tool_call_id === toolCall.id);
    if (matched) return matched;
  }
  return toolMessages[index] ?? null;
}

function getInteractionToolItems(interaction: unknown, interactionIndex: number): ToolRenderItem[] {
  const record = asRecord(interaction);
  const assistant = asRecord(record?.assistant);
  const toolCalls = Array.isArray(assistant?.tool_calls) ? assistant.tool_calls as ToolCallLike[] : [];
  const toolMessages = Array.isArray(record?.tools) ? record.tools as ToolMessageLike[] : [];
  const items: ToolRenderItem[] = [];

  toolCalls.forEach((toolCall, callIndex) => {
    items.push(makeToolItem(
      toolCall,
      findToolMessage(toolMessages, toolCall, callIndex),
      `interaction-${interactionIndex}-${callIndex}`,
    ));
  });

  if (toolCalls.length === 0) {
    toolMessages.forEach((toolMessage, toolIndex) => {
      items.push(makeToolItem(null, toolMessage, `interaction-${interactionIndex}-tool-${toolIndex}`));
    });
  }

  return items;
}

function getAssistantToolItems(message: {
  tool_interactions?: unknown[];
  tool_calls?: unknown[];
  tool_results?: unknown[];
}): ToolRenderItem[] {
  const items: ToolRenderItem[] = [];

  if (Array.isArray(message.tool_interactions)) {
    message.tool_interactions.forEach((interaction, interactionIndex) => {
      items.push(...getInteractionToolItems(interaction, interactionIndex));
    });
  }

  if (items.length > 0) return items;

  const toolCalls = Array.isArray(message.tool_calls) ? message.tool_calls as ToolCallLike[] : [];
  const toolResults = Array.isArray(message.tool_results) ? message.tool_results as ToolMessageLike[] : [];
  toolCalls.forEach((toolCall, callIndex) => {
    items.push(makeToolItem(
      toolCall,
      findToolMessage(toolResults, toolCall, callIndex),
      `call-${callIndex}`,
    ));
  });
  return items;
}

type AssistantTimelineBlock =
  | { type: 'reasoning'; key: string; reasoning: string }
  | { type: 'content'; key: string; content: string }
  | { type: 'tools'; key: string; items: ToolRenderItem[] };

type QueuedMessage = {
  id: string;
  conversationId: string;
  content: string;
  request: SendMessageRequest;
};

function createQueuedMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `queued-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function stripChronologicalPrefix(raw: unknown, snippets: string[]): string {
  if (typeof raw !== 'string' || raw.length === 0) return '';
  let remaining = raw;
  for (const snippet of snippets) {
    if (snippet && remaining.startsWith(snippet)) {
      remaining = remaining.slice(snippet.length);
    }
  }
  return remaining;
}

function getAssistantTimeline(message: {
  content?: unknown;
  reasoning?: unknown;
  tool_interactions?: unknown[];
  tool_calls?: unknown[];
  tool_results?: unknown[];
}): AssistantTimelineBlock[] {
  const blocks: AssistantTimelineBlock[] = [];
  const interactions = Array.isArray(message.tool_interactions) ? message.tool_interactions : [];

  if (interactions.length > 0) {
    const interactionReasoning: string[] = [];
    const interactionContent: string[] = [];

    interactions.forEach((interaction, interactionIndex) => {
      const record = asRecord(interaction);
      const assistant = asRecord(record?.assistant);
      const reasoning = typeof record?.reasoning === 'string' ? record.reasoning : '';
      const content = typeof assistant?.content === 'string' ? assistant.content : '';
      const toolItems = getInteractionToolItems(interaction, interactionIndex);

      if (reasoning) {
        interactionReasoning.push(reasoning);
        blocks.push({ type: 'reasoning', key: `reasoning-${interactionIndex}`, reasoning });
      }
      if (content.trim()) {
        interactionContent.push(content);
        blocks.push({ type: 'content', key: `content-${interactionIndex}`, content });
      }
      if (toolItems.length > 0) {
        blocks.push({ type: 'tools', key: `tools-${interactionIndex}`, items: toolItems });
      }
    });

    const finalReasoning = stripChronologicalPrefix(message.reasoning, interactionReasoning);
    const finalContent = stripChronologicalPrefix(message.content, interactionContent);
    if (finalReasoning.trim()) {
      blocks.push({ type: 'reasoning', key: 'reasoning-final', reasoning: finalReasoning });
    }
    if (finalContent.trim()) {
      blocks.push({ type: 'content', key: 'content-final', content: finalContent });
    }
    return blocks;
  }

  const reasoning = typeof message.reasoning === 'string' ? message.reasoning : '';
  const content = typeof message.content === 'string' ? message.content : '';
  const toolItems = getAssistantToolItems(message);
  if (reasoning) blocks.push({ type: 'reasoning', key: 'reasoning', reasoning });
  if (toolItems.length > 0) blocks.push({ type: 'tools', key: 'tools', items: toolItems });
  if (content.trim()) blocks.push({ type: 'content', key: 'content', content });
  return blocks;
}

function isToolMessageCovered(
  toolMessage: ToolMessageLike,
  previousMessages: Array<{ role?: string; tool_interactions?: unknown[]; tool_calls?: unknown[]; tool_results?: unknown[] }>,
): boolean {
  return previousMessages.some((message) => {
    if (message.role !== 'assistant') return false;
    const items = getAssistantToolItems(message);
    if (toolMessage.tool_call_id && items.some((item) => item.key === toolMessage.tool_call_id)) return true;
    return !toolMessage.tool_call_id && items.some((item) => item.name === (toolMessage.name || 'tool'));
  });
}

function ToolCallCard({ item }: { item: ToolRenderItem }) {
  const [expanded, setExpanded] = useState(false);
  const [fullResult, setFullResult] = useState<ToolResultSlice | null>(null);
  const [loadingFullResult, setLoadingFullResult] = useState(false);
  const [fullResultError, setFullResultError] = useState<string | null>(null);
  const fullResultText = fullResult ? formatToolOutput({ content: fullResult.content }) : '';
  const outputText = fullResult ? fullResultText : item.outputText;
  const canLoadFullResult = Boolean(item.resultEnvelope?.toolResultId && !fullResult);
  const resultStatus = fullResult
    ? fullResult.has_more
      ? `已读取 ${fullResult.content.length}/${fullResult.total_chars} 字，已截断/可继续读取`
      : `已读取完整结果（${fullResult.total_chars} 字）`
    : item.resultEnvelope?.truncated
      ? '预览已截断'
      : null;

  const handleLoadFullResult = async () => {
    const toolResultId = item.resultEnvelope?.toolResultId;
    if (!toolResultId || loadingFullResult) return;
    setLoadingFullResult(true);
    setFullResultError(null);
    try {
      const result = await messageApi.getToolResult(toolResultId, 0, 16000);
      setFullResult(result);
    } catch (error) {
      console.error('Failed to load tool result:', error);
      setFullResultError('读取完整结果失败');
    } finally {
      setLoadingFullResult(false);
    }
  };

  return (
    <div className={cn('tool-call', expanded && 'expanded')}>
      <button
        type="button"
        className="tc-header"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="tc-name">{item.name}</span>
        <span className="tc-summary">{item.summary}</span>
        <span className="tc-status" aria-label={item.status === 'done' ? '工具调用完成' : item.status === 'error' ? '工具调用失败' : '工具调用中'}>
          {item.status === 'done' && <Check className="h-3 w-3" style={{ color: 'var(--icon-accent)' }} />}
          {item.status === 'error' && <X className="h-3 w-3" style={{ color: 'var(--destructive, #ef4444)' }} />}
          {item.status === 'running' && <span className="pulsing-dot" />}
        </span>
        <ChevronRight className="tc-chevron" />
      </button>
      <div className="tc-body">
        {item.argsText && <pre className="tc-cmd custom-scrollbar">{item.argsText}</pre>}
        {outputText && <pre className="tc-output custom-scrollbar">{outputText}</pre>}
        {(canLoadFullResult || resultStatus || fullResultError) && (
          <div className="tool-approval-actions">
            {canLoadFullResult && (
              <Button
                type="button"
                size="xs"
                variant="secondary"
                disabled={loadingFullResult}
                onClick={handleLoadFullResult}
              >
                {loadingFullResult ? (
                  <><Loader2 className="h-3 w-3 animate-spin" /> 读取中</>
                ) : (
                  '读取完整结果'
                )}
              </Button>
            )}
            {resultStatus && (
              <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{resultStatus}</span>
            )}
            {fullResultError && (
              <span className="text-xs text-destructive">{fullResultError}</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ToolCallGroup({ items }: { items: ToolRenderItem[] }) {
  const [collapsed, setCollapsed] = useState(false);
  if (items.length === 0) return null;
  if (items.length === 1) return <ToolCallCard item={items[0]} />;
  return (
    <div className={cn('tool-group', collapsed && 'collapsed')}>
      <button
        type="button"
        className="tool-group-header"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((value) => !value)}
      >
        <ChevronRight className="tg-chevron" />
        <span>工具调用</span>
        <span className="tg-count">{items.length} 个</span>
      </button>
      <div className="tool-group-body">
        {items.map((item) => <ToolCallCard key={item.key} item={item} />)}
      </div>
    </div>
  );
}

function ToolApprovalCard({ approval }: { approval: ToolApprovalPayload }) {
  const [submittingAction, setSubmittingAction] = useState<string | null>(null);
  const toolName = approval.tool_name || 'tool';
  const risk = approval.risk || approval.risk_level || 'unknown';
  const reason = approval.reason || '';
  const argsPreview = approval.arguments_preview || '';
  const approvalSummary = summarizeToolCall(toolName, argsPreview);
  const approvalArgs = argsPreview ? formatToolArguments(argsPreview) : '';

  const handleDecision = async (
    decision: ToolApprovalDecision,
    scope: ToolApprovalScope,
    action: string,
  ) => {
    setSubmittingAction(action);
    try {
      await messageApi.decideApproval(approval.id, decision, scope);
    } catch (error) {
      console.error('Failed to decide tool approval:', error);
      setSubmittingAction(null);
    }
  };

  return (
    <div className="tool-call tool-approval-call">
      <div className="tc-header tool-approval-header">
        <span className="tc-name">{toolName}</span>
        <span className="tc-summary">{approvalSummary || reason || '等待审批'}</span>
        <span className="tool-approval-risk">{risk}</span>
      </div>
      <div className="tool-approval-body">
        {reason && <div className="tool-approval-reason">{reason}</div>}
        {approvalArgs && <pre className="tc-cmd custom-scrollbar">{approvalArgs}</pre>}
        <div className="tool-approval-actions">
          <Button
            type="button"
            size="xs"
            variant="secondary"
            disabled={submittingAction !== null}
            onClick={() => handleDecision('approve', 'once', 'approve-once')}
          >
            允许一次
          </Button>
          <Button
            type="button"
            size="xs"
            variant="secondary"
            disabled={submittingAction !== null}
            onClick={() => handleDecision('approve', 'session', 'approve-session')}
          >
            允许本会话
          </Button>
          <Button
            type="button"
            size="xs"
            variant="ghost"
            disabled={submittingAction !== null}
            onClick={() => handleDecision('deny', 'once', 'deny')}
          >
            拒绝
          </Button>
        </div>
      </div>
    </div>
  );
}

function ToolApprovalGroup({ approvals }: { approvals: ToolApprovalPayload[] }) {
  if (approvals.length === 0) return null;
  return (
    <div className="tool-group tool-approval-group">
      <div className="tool-group-header tool-approval-group-header">
        <ChevronRight className="tg-chevron" />
        <span>工具审批</span>
        <span className="tg-count">{approvals.length} 个</span>
      </div>
      <div className="tool-group-body">
        {approvals.map((approval) => (
          <ToolApprovalCard key={approval.id} approval={approval} />
        ))}
      </div>
    </div>
  );
}

/* ---------- Component ---------- */
export default function ChatPage() {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
  const [scrollPositions, setScrollPositions] = useState<Record<string, number>>({});
  const [isScrolling, setIsScrolling] = useState(false);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [editValue, setEditValue] = useState<string | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<string[]>([]);
  const [attachedImageRefs, setAttachedImageRefs] = useState<Array<{ filename: string; mime_type?: string }>>([]);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [previewFile, setPreviewFile] = useState<{ name: string; content: string } | null>(null);
  const [previewImage, setPreviewImage] = useState<{ name: string; url: string } | null>(null);
  const [conversationSearch, setConversationSearch] = useState('');
  const [projectPickerSearch, setProjectPickerSearch] = useState('');
  const [collapsedProjectIds, setCollapsedProjectIds] = useState<Set<string>>(() => new Set());
  const [expandedHistoryProjectIds, setExpandedHistoryProjectIds] = useState<Set<string>>(() => new Set());
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [manualProjectWorkspaces, setManualProjectWorkspaces] = useState<WorkspaceContext[]>(() => loadManualProjectWorkspaces());
  const [projectOrder, setProjectOrder] = useState<string[]>(() => loadProjectOrder());
  const [draggingProjectId, setDraggingProjectId] = useState<string | null>(null);
  const [projectFolderDialogMode, setProjectFolderDialogMode] = useState<'create' | 'existing' | null>(null);
  const [projectFolderPath, setProjectFolderPath] = useState('');
  const [projectFolderLabel, setProjectFolderLabel] = useState('');
  const [projectFolderError, setProjectFolderError] = useState('');
  const [projectFolderSubmitting, setProjectFolderSubmitting] = useState(false);
  const scrollTimeoutRef = useRef<number | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const conversationSearchInputRef = useRef<HTMLInputElement>(null);
  const pendingScrollId = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const userScrollingRef = useRef(false);
  const scrollEndTimeoutRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);

  const { chatViewMode, toggleChatViewMode, openSettings } = useNavigationStore();

  const updateQueuedMessages = useCallback((updater: (messages: QueuedMessage[]) => QueuedMessage[]) => {
    const next = updater(queuedMessagesRef.current);
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  const isAtBottom = useCallback(() => {
    if (!historyRef.current) return true;
    const { scrollTop, scrollHeight, clientHeight } = historyRef.current;
    return scrollHeight - scrollTop - clientHeight < 100;
  }, []);

  const scrollToBottom = useCallback((smooth = true) => {
    if (historyRef.current) {
      programmaticScrollRef.current = true;
      const container = historyRef.current;
      if (smooth) {
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
      } else {
        container.scrollTop = container.scrollHeight;
      }
    }
  }, []);

  const handleScroll = useCallback(() => {
    setIsScrolling(true);
    if (programmaticScrollRef.current) {
      programmaticScrollRef.current = false;
    } else {
      userScrollingRef.current = true;
      const atBottom = isAtBottom();
      setShouldAutoScroll(atBottom);
    }
    if (scrollTimeoutRef.current) clearTimeout(scrollTimeoutRef.current);
    scrollTimeoutRef.current = window.setTimeout(() => setIsScrolling(false), 1000);
    if (scrollEndTimeoutRef.current) clearTimeout(scrollEndTimeoutRef.current);
    scrollEndTimeoutRef.current = window.setTimeout(() => {
      userScrollingRef.current = false;
    }, 150);
  }, [isAtBottom]);

  const {
    conversations, currentConversation, messages,
    pendingScrollNodeId, clearPendingScroll,
    createConversation, selectConversation, deleteConversation, loadConversations,
    clearCurrentConversation, updateConversationTitle, refreshMessages, deleteNode,
  } = useConversationStore();

  const [renameDialogOpen, setRenameDialogOpen] = useState(false);
  const [renameConversationId, setRenameConversationId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState('');

  const handleRenameClick = (id: string, currentTitle: string) => {
    setRenameConversationId(id);
    setRenameTitle(currentTitle || '');
    setRenameDialogOpen(true);
  };

  const handleRenameConfirm = async () => {
    if (renameConversationId && renameTitle.trim()) {
      await updateConversationTitle(renameConversationId, renameTitle.trim());
    }
    setRenameDialogOpen(false);
    setRenameConversationId(null);
    setRenameTitle('');
  };

  const handleRenameCancel = () => {
    setRenameDialogOpen(false);
    setRenameConversationId(null);
    setRenameTitle('');
  };

  const {
    streamedContent, streamedReasoning, startStreaming, isStreaming, abortStreaming,
    streamedReasoningActive, streamedToolInteractions, pendingApprovals, streamDuration, streamStatus, streamErrorMessage, pendingUserMessage, currentNodeId: streamNodeId,
  } = useStreamingManager(currentConversation?.id ?? null);
  const [localStreamingConversationIds, setLocalStreamingConversationIds] = useState<Set<string>>(() => new Set());
  const [backendActiveStreamConversationIds, setBackendActiveStreamConversationIds] = useState<Set<string>>(() => new Set());
  const activeStreamConversationIds = useMemo(() => {
    return new Set([...localStreamingConversationIds, ...backendActiveStreamConversationIds]);
  }, [localStreamingConversationIds, backendActiveStreamConversationIds]);
  const projectDragMovedRef = useRef(false);
  const projectGroupRefs = useRef(new Map<string, HTMLDivElement>());
  const projectFlipFirstRef = useRef<Map<string, number> | null>(null);

  // 结构性去重：一旦本轮流式产生的节点已出现在真实消息里（refreshMessages 注入），
  // 就隐藏对应的乐观叠加层，无论 cleanup 何时执行。这样真实消息与乐观叠加层
  // 永远不会同时渲染同一轮，杜绝“重复两轮”。
  // 注意：后端在流式 START 时就已创建节点并保存 user 消息，但 assistant 消息要到
  // 结束才保存。因此必须按角色分别判断——否则中途重新进入正在流式的对话会
  // 把 user 消息拉回 messages，误判“整轮已落地”而把正在生成的助手块也隐藏掉。
  const userMsgLanded =
    streamNodeId != null && messages.some((m) => m.node_id === streamNodeId && m.role === 'user');
  const assistantMsgLanded =
    streamNodeId != null && messages.some((m) => m.node_id === streamNodeId && m.role === 'assistant');
  // 用户气泡：真实 user 消息已出现即隐藏。
  const showPendingBubble = !!pendingUserMessage && !userMsgLanded;
  // 助手流式块：仅当真实 assistant 消息已出现（=本轮已结束并保存）才隐藏，
  // 保证流式进行中（assistant 尚未保存）始终显示“思考中/流式内容/计时”。
  const showStreamBlock = streamStatus !== 'idle' && !assistantMsgLanded;
  const streamedTimeline = getAssistantTimeline({
    content: streamedContent,
    reasoning: streamedReasoning,
    tool_interactions: streamedToolInteractions,
  });
  const pendingApprovalList = Object.values(pendingApprovals).filter((approval) => approval.status === 'pending');
  const pendingApprovalCount = pendingApprovalList.length;
  const visibleQueuedMessages = useMemo(
    () => queuedMessages
      .filter((message) => message.conversationId === currentConversation?.id)
      .map(({ id, content }) => ({ id, content })),
    [queuedMessages, currentConversation?.id],
  );
  const defaultWorkspace = useMemo(
    () => conversations.find((conversation) => conversation.workspace?.cwd)?.workspace || null,
    [conversations],
  );
  const projectGroups = useMemo(
    () => groupConversationsByProject(conversations, {
      defaultWorkspace,
      extraWorkspaces: manualProjectWorkspaces,
      collapsedProjectIds,
      expandedHistoryProjectIds,
      searchQuery: conversationSearch,
      projectOrder,
    }),
    [conversations, defaultWorkspace, manualProjectWorkspaces, collapsedProjectIds, expandedHistoryProjectIds, conversationSearch, projectOrder],
  );
  const allProjectGroups = useMemo(
    () => groupConversationsByProject(conversations, {
      defaultWorkspace,
      extraWorkspaces: manualProjectWorkspaces,
      collapsedProjectIds,
      expandedHistoryProjectIds,
      projectOrder,
    }),
    [conversations, defaultWorkspace, manualProjectWorkspaces, collapsedProjectIds, expandedHistoryProjectIds, projectOrder],
  );
  const measureProjectGroupTops = useCallback((skipProjectId?: string) => {
    const tops = new Map<string, number>();
    for (const [projectId, element] of projectGroupRefs.current.entries()) {
      if (projectId === skipProjectId) continue;
      element.getAnimations().forEach((animation) => animation.cancel());
      tops.set(projectId, element.getBoundingClientRect().top);
    }
    return tops;
  }, []);

  useLayoutEffect(() => {
    const first = projectFlipFirstRef.current;
    if (!first) return;
    projectFlipFirstRef.current = null;

    window.requestAnimationFrame(() => {
      for (const group of projectGroups) {
        if (group.id === draggingProjectId) continue;
        const element = projectGroupRefs.current.get(group.id);
        const previousTop = first.get(group.id);
        if (!element || previousTop == null) continue;
        const delta = previousTop - element.getBoundingClientRect().top;
        if (!delta) continue;
        element.animate(
          [
            { transform: `translateY(${delta}px)` },
            { transform: 'translateY(0)' },
          ],
          { duration: 240, easing: 'cubic-bezier(.22,1,.36,1)' },
        );
      }
    });
  }, [draggingProjectId, projectGroups]);

  const selectedNewConversationWorkspace = useMemo(
    () => getWorkspaceForNewConversation(allProjectGroups, selectedProjectId, defaultWorkspace),
    [allProjectGroups, selectedProjectId, defaultWorkspace],
  );

  useEffect(() => {
    if (!allProjectGroups.length) return;
    if (!selectedProjectId || !allProjectGroups.some((group) => group.id === selectedProjectId)) {
      setSelectedProjectId(allProjectGroups[0].id);
    }
  }, [allProjectGroups, selectedProjectId]);

  useEffect(() => {
    if (!currentConversation?.workspace?.cwd) return;
    const group = allProjectGroups.find((item) => item.path === currentConversation.workspace?.cwd);
    if (group && group.id !== selectedProjectId) {
      setSelectedProjectId(group.id);
    }
  }, [currentConversation?.workspace?.cwd, allProjectGroups, selectedProjectId]);

  const toggleProjectCollapsed = (projectId: string) => {
    setCollapsedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const updateProjectOrder = useCallback((order: string[]) => {
    setProjectOrder(order);
    saveProjectOrder(order);
  }, []);

  const handleProjectDragStart = useCallback((event: DragEvent, projectId: string) => {
    projectDragMovedRef.current = false;
    setDraggingProjectId(projectId);
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', projectId);
  }, []);

  const handleProjectDragOver = useCallback((event: DragEvent, targetProjectId: string) => {
    if (!draggingProjectId || draggingProjectId === targetProjectId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';

    const ids = allProjectGroups.map((group) => group.id);
    const fromIndex = ids.indexOf(draggingProjectId);
    const targetIndex = ids.indexOf(targetProjectId);
    if (fromIndex < 0 || targetIndex < 0) return;

    const rect = event.currentTarget.getBoundingClientRect();
    const insertAfterTarget = event.clientY > rect.top + rect.height / 2;
    const withoutDragged = ids.filter((id) => id !== draggingProjectId);
    const targetIndexAfterRemoval = withoutDragged.indexOf(targetProjectId);
    const insertIndex = targetIndexAfterRemoval + (insertAfterTarget ? 1 : 0);
    withoutDragged.splice(insertIndex, 0, draggingProjectId);

    if (withoutDragged.join('\u0000') !== ids.join('\u0000')) {
      projectDragMovedRef.current = true;
      projectFlipFirstRef.current = measureProjectGroupTops(draggingProjectId);
      updateProjectOrder(withoutDragged);
    }
  }, [allProjectGroups, draggingProjectId, measureProjectGroupTops, updateProjectOrder]);

  const handleProjectDragEnd = useCallback(() => {
    setDraggingProjectId(null);
    window.setTimeout(() => {
      projectDragMovedRef.current = false;
    }, 100);
  }, []);

  const toggleHistoryExpanded = (projectId: string) => {
    setExpandedHistoryProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const workspaceForCreateRequest = () => (
    selectedNewConversationWorkspace.cwd ? selectedNewConversationWorkspace : undefined
  );

  const rememberProjectWorkspace = (workspace: WorkspaceContext) => {
    const next = mergeManualProjectWorkspace(manualProjectWorkspaces, workspace);
    setManualProjectWorkspaces(next);
    saveManualProjectWorkspaces(next);
    setSelectedProjectId(encodeProjectId(workspace.cwd));
  };

  const openProjectFolderDialog = (mode: 'create' | 'existing') => {
    setProjectFolderDialogMode(mode);
    setProjectFolderPath('');
    setProjectFolderLabel('');
    setProjectFolderError('');
  };

  const closeProjectFolderDialog = () => {
    if (projectFolderSubmitting) return;
    setProjectFolderDialogMode(null);
    setProjectFolderPath('');
    setProjectFolderLabel('');
    setProjectFolderError('');
  };

  const handleProjectFolderSubmit = async () => {
    const path = projectFolderPath.trim();
    if (!projectFolderDialogMode || !path) {
      setProjectFolderError('请输入文件夹路径');
      return;
    }
    setProjectFolderSubmitting(true);
    setProjectFolderError('');
    try {
      const label = projectFolderLabel.trim() || undefined;
      const workspace = projectFolderDialogMode === 'create'
        ? await conversationApi.createProjectFolder(path, label)
        : await conversationApi.resolveProjectFolder(path, label);
      rememberProjectWorkspace(workspace);
      setProjectPickerSearch('');
      setProjectFolderDialogMode(null);
      setProjectFolderPath('');
      setProjectFolderLabel('');
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || '项目文件夹处理失败';
      setProjectFolderError(String(detail));
    } finally {
      setProjectFolderSubmitting(false);
    }
  };

  const selectedProjectGroup = allProjectGroups.find((group) => group.id === selectedProjectId) || allProjectGroups[0] || null;
  const filteredProjectGroups = projectPickerSearch.trim()
    ? allProjectGroups.filter((group) => {
        const query = projectPickerSearch.trim().toLowerCase();
        return `${group.label} ${group.path}`.toLowerCase().includes(query);
      })
    : allProjectGroups;

  const projectSettingsSlot = (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="new-chat-setting-chip"
          title={selectedProjectGroup?.path || selectedNewConversationWorkspace.cwd || '默认项目'}
        >
          <FolderOpen className="h-4 w-4" />
          <span className="truncate">{selectedProjectGroup?.label || selectedNewConversationWorkspace.label || '默认项目'}</span>
          <ChevronRight className="h-3.5 w-3.5 rotate-90 opacity-70" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="new-chat-project-menu">
        <div className="p-2">
          <Input
            value={projectPickerSearch}
            onChange={(event) => setProjectPickerSearch(event.target.value)}
            placeholder="搜索项目"
            className="h-8 text-xs"
          />
        </div>
        <div className="max-h-[260px] overflow-y-auto custom-scrollbar px-1 pb-1">
          {filteredProjectGroups.map((group) => (
            <button
              type="button"
              key={group.id}
              className="new-chat-project-option"
              onClick={() => {
                setSelectedProjectId(group.id);
                setProjectPickerSearch('');
              }}
              title={group.path}
            >
              <FolderOpen className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 text-left">
                <span className="block truncate text-sm">{group.label}</span>
                <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                  {group.path}
                </span>
              </span>
              {selectedProjectId === group.id && <Check className="h-4 w-4 shrink-0" />}
            </button>
          ))}
          {filteredProjectGroups.length === 0 && (
            <div className="px-3 py-3 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              没有匹配的项目
            </div>
          )}
        </div>
        <div className="border-t px-1 py-1" style={{ borderColor: 'var(--border)' }}>
          <button
            type="button"
            className="new-chat-project-option"
            onClick={() => openProjectFolderDialog('create')}
          >
            <FolderPlus className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-sm">新建文件夹</span>
              <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                创建空项目
              </span>
            </span>
          </button>
          <button
            type="button"
            className="new-chat-project-option"
            onClick={() => openProjectFolderDialog('existing')}
          >
            <FolderOpen className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-sm">使用现有文件夹</span>
              <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                添加为项目
              </span>
            </span>
          </button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );

  const handleNewConversation = () => {
    clearCurrentConversation();
  };
  const streamedActiveReasoningIndex = (() => {
    if (streamStatus !== 'streaming') return -1;
    for (let i = streamedTimeline.length - 1; i >= 0; i -= 1) {
      if (streamedTimeline[i].type === 'reasoning') {
        const hasLaterBlock = streamedTimeline.slice(i + 1).some((block) => block.type !== 'reasoning');
        return streamedReasoningActive || !hasLaterBlock ? i : -1;
      }
    }
    return -1;
  })();

  const sendNextQueuedMessage = useCallback(async (conversationId: string) => {
    const nextMessage = queuedMessagesRef.current.find((message) =>
      message.conversationId === conversationId && message.content.trim()
    );
    if (!nextMessage) {
      updateQueuedMessages((messages) =>
        messages.filter((message) => message.conversationId !== conversationId || message.content.trim())
      );
      return;
    }

    updateQueuedMessages((messages) => messages.filter((message) => message.id !== nextMessage.id));
    setShouldAutoScroll(true);
    await startStreaming(
      conversationId,
      {
        ...nextMessage.request,
        content: nextMessage.content,
      },
      nextMessage.content,
    );
  }, [startStreaming, updateQueuedMessages]);

  const handleUpdateQueuedMessage = useCallback((id: string, content: string) => {
    updateQueuedMessages((messages) =>
      messages.map((message) => message.id === id ? { ...message, content } : message)
    );
  }, [updateQueuedMessages]);

  const handleDeleteQueuedMessage = useCallback((id: string) => {
    updateQueuedMessages((messages) => messages.filter((message) => message.id !== id));
  }, [updateQueuedMessages]);

  const handleStopStreaming = useCallback(() => {
    if (currentConversation?.id) {
      const conversationId = currentConversation.id;
      updateQueuedMessages((messages) => messages.filter((message) => message.conversationId !== conversationId));
    }
    abortStreaming();
  }, [abortStreaming, currentConversation?.id, updateQueuedMessages]);

  // 全局注册一次：任意对话的流结束（completed/error/stopped）时，
  // 从后端刷新真实消息，再清理 StreamManager 中该对话的临时状态。
  // 不依赖当前查看的是哪个对话，因此切走的对话流完成也能正确落地。
  useEffect(() => {
    const unsubscribe = streamManager.onFinish(async ({ conversationId: finishedId, drained, nodeId, controller }) => {
      // 完成判据：等待本轮节点(nodeId)的 assistant 消息落盘，而非“消息数 +1”。
      // 对多消息轮次（未来工具轮次）同样稳健。nodeId 为空（停得太早还没拿到）时
      // refreshMessages 退化为单次拉取。
      // drained=true：后端在 [DONE] 前已保存，一次即可拿到最终结果。
      // drained=false（硬 abort）：保存由连接断开触发，与刷新竞态，需轮询重试，
      //   期间保留乐观气泡，避免“用户消息瞬间消失”。
      const confirmed = await refreshMessages(
        finishedId,
        drained
          ? (nodeId ? { awaitNodeId: nodeId, retries: 0 } : undefined)
          : { awaitNodeId: nodeId ?? undefined, retries: 6 },
      );
      // 仅当确认真实消息已落地，才清理临时流状态（移除乐观气泡）。
      // 身份校验：若 await 期间用户对同一对话发起了新流，controller 已被替换则跳过。
      if (drained || confirmed) {
        streamManager.cleanupIfController(finishedId, controller);
      } else {
        // 硬 abort 且后端保存超过重试预算：保留乐观气泡，延后再确认一次，
        // 成功后再清理，彻底避免用户消息闪失。
        setTimeout(async () => {
          await refreshMessages(finishedId, { awaitNodeId: nodeId ?? undefined, retries: 6 });
          // 无论是否确认，这是最后兜底：清理临时状态，避免气泡永久残留。
          streamManager.cleanupIfController(finishedId, controller);
        }, 800);
      }
      // 同步对话列表（更新时间、标题等）
      await loadConversations();
      await sendNextQueuedMessage(finishedId);
    });
    return unsubscribe;
  }, [refreshMessages, loadConversations, sendNextQueuedMessage]);

  const shouldAutoScrollRef = useRef(shouldAutoScroll);
  shouldAutoScrollRef.current = shouldAutoScroll;

  useEffect(() => {
    if (isStreaming && shouldAutoScrollRef.current && !userScrollingRef.current) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [streamedContent, isStreaming, scrollToBottom]);

  useEffect(() => {
    if (pendingUserMessage) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [pendingUserMessage, scrollToBottom]);

  useEffect(() => {
    if (pendingApprovalCount > 0) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [pendingApprovalCount, scrollToBottom]);

  useEffect(() => {
    loadConversations();
  }, []);

  useEffect(() => {
    const updateLocalStreamingIds = () => {
      setLocalStreamingConversationIds(new Set(streamManager.getStreamingConversationIds()));
    };
    updateLocalStreamingIds();
    return streamManager.subscribe(updateLocalStreamingIds);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const syncBackendActiveStreams = async () => {
      try {
        const activeStreams = await messageApi.getAllActiveStreams();
        if (cancelled) return;
        setBackendActiveStreamConversationIds(
          new Set(activeStreams.map((item) => item.conversation_id).filter(Boolean)),
        );
      } catch (_) {
        if (!cancelled) setBackendActiveStreamConversationIds(new Set());
      }
    };
    void syncBackendActiveStreams();
    const timer = window.setInterval(syncBackendActiveStreams, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) return;

    let cancelled = false;
    (async () => {
      try {
        let active = null;
        for (let attempt = 0; attempt < 10; attempt += 1) {
          const activeStreams = await messageApi.getActiveStreams(conversationId);
          if (cancelled) return;
          active = activeStreams.find((item) => item.node_id && !item.done) ?? null;
          if (active) break;
          await new Promise((resolve) => window.setTimeout(resolve, 500));
        }
        if (cancelled) return;
        if (!active) {
          await refreshMessages(conversationId, { retries: 0 });
          return;
        }
        if (!active?.node_id) return;
        if (streamManager.isStreaming(conversationId)) return;

        await refreshMessages(conversationId, {
          awaitNodeId: active.node_id,
          awaitRole: 'user',
          retries: 0,
        });
        if (cancelled) return;
        void streamManager.resumeStream(conversationId, active.node_id);
      } catch (_) {
        await refreshMessages(conversationId, { retries: 0 });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [currentConversation?.id, refreshMessages]);

  const handleSelectConversation = async (id: string) => {
    if (currentConversation && historyRef.current) {
      setScrollPositions(prev => ({
        ...prev,
        [currentConversation.id]: historyRef.current!.scrollTop
      }));
    }
    pendingScrollId.current = id;
    const selected = conversations.find((conversation) => conversation.id === id);
    if (selected?.workspace?.cwd) {
      const group = allProjectGroups.find((item) => item.path === selected.workspace?.cwd);
      if (group) setSelectedProjectId(group.id);
    }
    await selectConversation(id);
  };

  useLayoutEffect(() => {
    if (pendingScrollId.current && historyRef.current && currentConversation?.id === pendingScrollId.current) {
      const savedPosition = scrollPositions[pendingScrollId.current];
      if (savedPosition !== undefined) {
        historyRef.current.scrollTop = savedPosition;
      } else {
        historyRef.current.scrollTop = historyRef.current.scrollHeight;
      }
      pendingScrollId.current = null;
      setShouldAutoScroll(true);
    }
  }, [currentConversation, messages, scrollPositions]);

  // 从树视图双击跳转：等待消息渲染后滚动到目标节点
  useEffect(() => {
    if (!pendingScrollNodeId || chatViewMode !== 'chat') return;
    const idx = messages.findIndex((m) => m.node_id === pendingScrollNodeId);
    if (idx === -1) return;
    const tryScroll = () => {
      const el = document.getElementById('message-' + idx);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        clearPendingScroll();
      } else {
        requestAnimationFrame(tryScroll);
      }
    };
    requestAnimationFrame(tryScroll);
  }, [pendingScrollNodeId, messages, chatViewMode, clearPendingScroll]);

  const handleExportMarkdown = () => {
    if (!messages.length || !currentConversation) return;
    const title = currentConversation.title || '未命名对话';
    const lines: string[] = [];
    lines.push(`# ${title}`);
    lines.push('');
    for (const m of messages) {
      const displayContent = m.role === 'user' ? getUserDisplayContent(m) : m.content;
      const importFiles = m.role === 'user' ? getUserAttachmentNames(m) : [];
      const roleLabel = m.role === 'user' ? '**User**' : '**Assistant**';
      lines.push(`### ${roleLabel}`);
      lines.push('');
      for (const filename of importFiles) {
        lines.push(`- 附件: ${filename}`);
      }
      if (importFiles.length > 0) lines.push('');
      lines.push(displayContent);
      lines.push('');
      lines.push('---');
      lines.push('');
    }
    const md = lines.join('\n');
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${title}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleFilesPicked = async (files: File[]) => {
    let convId = currentConversation?.id;
    if (!convId) {
      const newConv = await createConversation({
        title: files[0]?.name?.slice(0, 20) || 'New',
        workspace: workspaceForCreateRequest(),
      });
      if (!newConv) return;
      convId = newConv.id;
    }
    for (const file of files) {
      try {
        const res = await conversationApi.uploadImport(convId, file);
        if (res.kind === 'image') {
          setAttachedImageRefs(prev => prev.some(ref => ref.filename === res.filename)
            ? prev
            : [...prev, { filename: res.filename, mime_type: res.mime_type ?? file.type }]);
        } else {
          setAttachedFiles(prev => prev.includes(res.filename) ? prev : [...prev, res.filename]);
        }
      } catch (err: any) {
        console.error('Upload failed:', err?.response?.data?.detail || err.message);
      }
    }
  };

  const handleRemoveFile = async (filename: string) => {
    if (!currentConversation) return;
    try {
      await conversationApi.deleteImport(currentConversation.id, filename);
    } catch (_) {}
    setAttachedFiles(prev => prev.filter(f => f !== filename));
    setAttachedImageRefs(prev => prev.filter(ref => ref.filename !== filename));
  };

  const handlePreviewFile = async (filename: string) => {
    if (!currentConversation) return;
    try {
      const resp = await fetch(`/api/conversations/${currentConversation.id}/imports/${encodeURIComponent(filename)}`);
      if (resp.ok) {
        const text = await resp.text();
        setPreviewFile({ name: filename, content: text });
      }
    } catch (_) {}
  };

  const getImportAssetUrl = (filename: string, conversationId = currentConversation?.id) => {
    if (!conversationId) return '';
    return `/api/conversations/${conversationId}/imports/${encodeURIComponent(filename)}`;
  };

  const handlePreviewImage = (filename: string) => {
    const url = getImportAssetUrl(filename);
    if (!url) return;
    setPreviewImage({ name: filename, url });
  };

  const handleSend = async (
    val: string,
    modelId?: string,
    providerId?: string,
    toolPermissionMode: ToolPermissionMode = 'modify_only',
  ) => {
    if (!val.trim()) return;
    setShouldAutoScroll(true);

    let conversationId = currentConversation?.id;
    const importFiles = attachedFiles.map(filename => ({ filename }));
    const imageRefs = attachedImageRefs.map(({ filename, mime_type }) => ({ filename, mime_type }));
    const clearAttachments = () => {
      if (importFiles.length > 0) setAttachedFiles([]);
      if (imageRefs.length > 0) setAttachedImageRefs([]);
    };
    const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
    const request: SendMessageRequest = {
      content: val,
      model_id: modelId,
      provider_id: providerId,
      reasoning_effort: currentReasoningEffort,
      thinking_enabled: currentThinkingEnabled,
      tool_permission_mode: toolPermissionMode,
      import_files: importFiles.length > 0 ? importFiles : undefined,
      image_refs: imageRefs.length > 0 ? imageRefs : undefined,
    };

    if (isStreaming) {
      if (!conversationId) return;
      const queuedConversationId = conversationId;
      clearAttachments();
      updateQueuedMessages((messages) => [
        ...messages,
        {
          id: createQueuedMessageId(),
          conversationId: queuedConversationId,
          content: val,
          request,
        },
      ]);
      return;
    }

    if (!conversationId) {
      const newConv = await createConversation({
        title: val.slice(0, 20),
        workspace: workspaceForCreateRequest(),
      });
      if (!newConv) {
        console.error('Failed to create conversation');
        return;
      }
      conversationId = newConv.id;
    }

    clearAttachments();
    // 第三个参数是乐观渲染的用户气泡文本（显示用户输入的原文）。
    // 推理设置从 modelStore 的当前值读取（已确认值），随请求透传。
    await startStreaming(
      conversationId,
      request,
      val,
    );
  };

  const handleJumpToMessage = (index: number) => {
    const element = document.getElementById(`message-${index}`);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  const handleCopy = async (content: string, messageId: string) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopiedMessageId(messageId);
      setTimeout(() => setCopiedMessageId(null), 2000);
    } catch {
      // ignore
    }
  };

  const handleDeleteBranch = async (nodeId: string) => {
    if (!currentConversation || isStreaming) return;
    if (!confirm('确定删除该消息及其所有后续分支？')) return;
    try {
      await deleteNode(nodeId);
    } catch (err) {
      console.error('删除失败:', err);
    }
  };

  const handleRetry = async (
    assistantNodeId: string,
    userContent: string,
    importFileNames: string[] = [],
    imageRefs: Array<{ filename: string; mime_type?: string }> = [],
  ) => {
    if (!currentConversation || isStreaming) return;
    const convId = currentConversation.id;
    try {
      await conversationApi.deleteNode(convId, assistantNodeId);
      await selectConversation(convId);
      setShouldAutoScroll(true);
      const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
      await startStreaming(
        convId,
        {
          content: userContent,
          reasoning_effort: currentReasoningEffort,
          thinking_enabled: currentThinkingEnabled,
          tool_permission_mode: 'modify_only',
          import_files: importFileNames.length > 0
            ? importFileNames.map(filename => ({ filename }))
            : undefined,
          image_refs: imageRefs.length > 0 ? imageRefs : undefined,
        },
        userContent,
      );
    } catch (err) {
      console.error('重试失败:', err);
      await selectConversation(convId);
    }
  };

  const handleEditUserMessage = async (_nodeId: string, parentNodeId: string | undefined, userContent: string) => {
    if (!currentConversation || isStreaming) return;
    if (!parentNodeId) return;
    try {
      await conversationApi.switchNode(currentConversation.id, parentNodeId);
      await selectConversation(currentConversation.id);
      setEditValue(userContent);
    } catch (err) {
      console.error('编辑失败:', err);
    }
  };

  const parseFileMention = (content: string): { fileNames: string[]; cleanContent: string } | null => {
    const match = content.match(/^'''USER MENTIONED FILES:\s+(.*?)\s+'''\n\n[\s\S]*?\n---\n\n/s);
    if (!match) return null;
    const fileNames = match[1].split(/\s+/).filter(Boolean);
    const cleanContent = content.slice(match[0].length);
    return { fileNames, cleanContent };
  };

  const getUserImportFileNames = (message: typeof messages[0]): string[] => {
    const structured = (message.import_files ?? [])
      .map((file) => file.filename)
      .filter(Boolean);
    if (structured.length > 0) return structured;
    return parseFileMention(message.content)?.fileNames ?? [];
  };

  const getUserImageRefs = (message: typeof messages[0]): Array<{ filename: string; mime_type?: string }> => {
    return (message.image_refs ?? [])
      .filter((file) => Boolean(file.filename));
  };

  const getUserAttachmentNames = (message: typeof messages[0]): string[] => {
    return [
      ...getUserImportFileNames(message),
      ...getUserImageRefs(message).map(file => file.filename),
    ];
  };

  const getUserDisplayContent = (message: typeof messages[0]): string => {
    return parseFileMention(message.content)?.cleanContent ?? message.content;
  };

  const isCompactBoundaryMessage = (message: typeof messages[0]) =>
    message.role === 'system' && message.subtype === 'compact_boundary';

  const isCompactSummaryMessage = (message: typeof messages[0]) =>
    message.is_compact_summary === true;

  const formatCompactTokens = (tokens?: number) => {
    if (!tokens || tokens <= 0) return null;
    if (tokens >= 1000) return `${Math.round(tokens / 1000)}k tokens`;
    return `${tokens} tokens`;
  };

  const formatCompactTrigger = (trigger?: string) => {
    if (trigger === 'auto') return '自动压缩';
    if (trigger === 'manual') return '手动压缩';
    return '上下文压缩';
  };

  const formatRestoredFiles = (count?: number) => {
    if (!count) return null;
    return `恢复 ${count} 个文件`;
  };

  const outline = messages
    .map((m, index) => ({ ...m, originalIndex: index }))
    .filter((m) => m.role === 'user' && !isCompactSummaryMessage(m))
    .map((m) => {
      const clean = getUserDisplayContent(m);
      return {
        text: clean.slice(0, 20) + (clean.length > 20 ? '...' : ''),
        originalIndex: m.originalIndex,
      };
    });

  const formatDuration = (ms: number): string => {
    if (ms < 1000) return `${ms}ms`;
    const seconds = Math.floor(ms / 1000);
    const remainingMs = ms % 1000;
    if (seconds < 60) return remainingMs > 0 ? `${seconds}.${Math.floor(remainingMs / 100)}s` : `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = seconds % 60;
    return `${minutes}m ${remainingSeconds}s`;
  };

  const getStreamStatusText = (): string | null => {
    return getStreamStatusLabel(streamStatus, streamErrorMessage);
  };

  // Parse '''USER MENTIONED FILES: ...''' prefix from message content

  const renderMsg = (m: typeof messages[0], index: number) => {
    if (isCompactBoundaryMessage(m)) {
      const trigger = formatCompactTrigger(m.compact_metadata?.trigger);
      const tokens = formatCompactTokens(m.compact_metadata?.pre_tokens);
      const restoredFiles = formatRestoredFiles(m.compact_metadata?.restored_files?.length);
      return (
        <div
          key={m.id}
          id={`message-${index}`}
          className="w-full my-4 flex items-center justify-center"
        >
          <div
            className="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs"
            style={{
              color: 'var(--fg-tertiary)',
              border: '0.5px solid var(--border)',
              background: 'var(--bg-button-tertiary-hover)',
            }}
          >
            <Archive className="h-3.5 w-3.5" />
            <span>{trigger}</span>
            {tokens && <span style={{ color: 'var(--fg-tertiary)' }}>{tokens}</span>}
            {restoredFiles && <span style={{ color: 'var(--fg-tertiary)' }}>{restoredFiles}</span>}
          </div>
        </div>
      );
    }

    if (isCompactSummaryMessage(m)) {
      return (
        <div
          key={m.id}
          id={`message-${index}`}
          className="w-full my-2 flex flex-col items-center"
        >
          <details
            className="w-full max-w-[720px] rounded-lg px-3 py-2 text-sm"
            style={{
              border: '0.5px solid var(--border)',
              background: 'var(--bg-secondary)',
              color: 'var(--fg-secondary)',
            }}
          >
            <summary className="cursor-pointer text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
              压缩摘要（transcript）
            </summary>
            <div className="mt-2 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2">
              <MarkdownView content={m.content} enableMermaid />
            </div>
          </details>
        </div>
      );
    }

    if (m.role === 'tool') {
      if (isToolMessageCovered(m, messages.slice(0, index))) return null;
      return (
        <div
          key={m.id}
          id={`message-${index}`}
          className="w-full my-2 flex flex-col group items-start"
        >
          <div className="flex flex-col items-start max-w-full">
            <ToolCallGroup items={[makeToolItem(null, m, `standalone-${m.id}`)]} />
          </div>
        </div>
      );
    }

    const prevUserMessage = index > 0 && messages[index - 1]?.role === 'user'
      ? messages[index - 1]
      : null;
    const fileNames = m.role === 'user' ? getUserImportFileNames(m) : [];
    const imageRefs = m.role === 'user' ? getUserImageRefs(m) : [];
    const displayContent = m.role === 'user' ? getUserDisplayContent(m) : m.content;
    const assistantTimeline = m.role === 'assistant' ? getAssistantTimeline(m) : [];
    const hasDisplayContent = displayContent.trim().length > 0;

    return (
      <div
        key={m.id}
        id={`message-${index}`}
        className={cn(
          'w-full my-2 flex flex-col group',
          m.role === 'user' ? 'items-end' : 'items-start',
        )}
      >
        <div className="flex flex-col items-start max-w-full">
          {imageRefs.length > 0 && (
            <div className="mb-1.5 flex max-w-full flex-wrap gap-2">
              {imageRefs.map((image) => {
                const imageUrl = getImportAssetUrl(image.filename);
                return (
                  <button
                    key={image.filename}
                    type="button"
                    className="h-24 w-24 overflow-hidden rounded-md border p-0 cursor-zoom-in transition-opacity hover:opacity-90"
                    style={{ borderColor: 'var(--border)', background: 'var(--bg-button-tertiary-hover)' }}
                    onClick={() => handlePreviewImage(image.filename)}
                    title={image.filename}
                  >
                    <img
                      src={imageUrl}
                      alt={image.filename}
                      className="h-full w-full object-cover"
                      loading="lazy"
                    />
                  </button>
                );
              })}
            </div>
          )}
          {fileNames.length > 0 && (
            <div className="max-w-full w-fit mb-1 px-2.5 py-1.5 rounded-lg text-xs flex flex-wrap items-center gap-1.5"
                 style={{ background: 'var(--accent-soft)', border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
              <FileText className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
              {fileNames.map((fn, fi) => (
                <span key={fi} className="px-1.5 py-0.5 rounded text-[11px] font-medium cursor-pointer transition-colors"
                      style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-active)'; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--accent-soft)'; }}
                      onClick={() => handlePreviewFile(fn)}>{fn}</span>
              ))}
            </div>
          )}
          {m.role === 'assistant' && assistantTimeline.map((block) => {
            if (block.type === 'reasoning') {
              return <ThinkingBlock key={block.key} reasoning={block.reasoning} />;
            }
            if (block.type === 'tools') {
              return <ToolCallGroup key={block.key} items={block.items} />;
            }
            return (
              <div
                key={block.key}
                className="max-w-full w-fit px-3 py-2 rounded-2xl leading-relaxed prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
                style={{
                  color: 'var(--fg-secondary)',
                  fontSize: 'var(--codex-chat-font-size)',
                  lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                }}
              >
                <MarkdownView content={block.content} enableMermaid />
              </div>
            );
          })}
          {m.role !== 'assistant' && hasDisplayContent && (
            <div
              className={cn(
                'max-w-full w-fit px-3 py-2 rounded-2xl leading-relaxed prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2',
                m.role === 'user'
                  ? 'prose-invert rounded-br-sm'
                  : ''
              )}
              style={
                m.role === 'user'
                  ? {
                      background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
                      border: '0.5px solid rgba(217,119,87,0.28)',
                      boxShadow: 'var(--highlight-top)',
                      color: 'var(--fg-85)',
                      fontSize: 'var(--codex-chat-font-size)',
                      lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                    }
                  : {
                      color: 'var(--fg-secondary)',
                      fontSize: 'var(--codex-chat-font-size)',
                      lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                    }
              }
            >
              <MarkdownView content={displayContent} enableMermaid />
            </div>
          )}
          {m.role === 'assistant' && m.generation_info && (
            <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
              <span>{formatDuration(m.generation_info.duration_ms)}</span>
              {m.generation_info.status !== 'completed' && (
                <span className={cn(
                  m.generation_info.status === 'error' ? 'text-destructive' : 'text-amber-500'
                )}>
                  {getGenerationStatusText(m.generation_info)}
                </span>
              )}
            </div>
          )}
          <div className="flex items-center gap-1 mt-1">
            <Button
              variant="ghost"
              size="sm"
              className="opacity-0 group-hover:opacity-100 transition-opacity h-7 w-7 p-0"
              onClick={() => handleCopy(displayContent, m.id)}
              aria-label="复制消息"
            >
              {copiedMessageId === m.id ? (
                <Check className="h-4 w-4" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
            {m.role === 'user' && (
              <Button
                variant="ghost"
                size="sm"
                className="opacity-0 group-hover:opacity-100 transition-opacity h-7 w-7 p-0"
                onClick={() => handleEditUserMessage(m.node_id, m.parent_node_id, displayContent)}
                disabled={isStreaming}
                aria-label="编辑"
                title="编辑消息（创建新分支）"
              >
                <Pencil className="h-4 w-4" />
              </Button>
            )}
            {m.role === 'user' && (
              <Button
                variant="ghost"
                size="sm"
                className="opacity-0 group-hover:opacity-100 transition-opacity h-7 w-7 p-0 text-destructive hover:text-destructive"
                onClick={() => handleDeleteBranch(m.node_id)}
                disabled={isStreaming}
                aria-label="删除分支"
                title="删除此消息及所有后续分支"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
            {m.role === 'assistant' && prevUserMessage && index === messages.length - 1 && (
              <Button
                variant="ghost"
                size="sm"
                className="opacity-0 group-hover:opacity-100 transition-opacity h-7 w-7 p-0"
                onClick={() => handleRetry(
                  m.node_id,
                  getUserDisplayContent(prevUserMessage),
                  getUserImportFileNames(prevUserMessage),
                  getUserImageRefs(prevUserMessage),
                )}
                disabled={isStreaming}
                aria-label="重试"
                title="重试（删除当前回复并重新生成）"
              >
                <RotateCcw className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-full" style={{ background: 'var(--bg-surface)' }}>
      {/* Left conversation list (collapsible) */}
      <nav
        className={cn('app-sidebar', sidebarCollapsed && 'app-sidebar-collapsed')}
        style={{ width: sidebarCollapsed ? '56px' : '300px' }}
      >
        <div className="app-sidebar-topbar">
          {!sidebarCollapsed && (
            <button
              type="button"
              className={cn('app-new-chat-action', !currentConversation && 'is-active')}
              onClick={handleNewConversation}
              title="新对话"
            >
              <Plus className="h-4 w-4 shrink-0" />
              <span>新对话</span>
            </button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="app-panel-toggle"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            {sidebarCollapsed ? <PanelLeftOpen className="h-5 w-5" /> : <PanelLeftClose className="h-5 w-5" />}
          </Button>
        </div>

        {!sidebarCollapsed && (
          <>
            <div className="app-sidebar-search">
              <Search className="h-3.5 w-3.5 shrink-0" />
              <Input
                ref={conversationSearchInputRef}
                value={conversationSearch}
                onChange={(event) => setConversationSearch(event.target.value)}
                placeholder="搜索项目或对话"
                className="h-8 border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-0"
              />
            </div>
            <div className="app-sidebar-project-heading">项目</div>
            <div className="app-sidebar-projects custom-scrollbar">
              {projectGroups.map((group) => {
                const visible = getVisibleProjectConversations(group);
                const selectedProject = selectedProjectId === group.id && !currentConversation;
                return (
                  <div
                    key={group.id}
                    ref={(element) => {
                      if (element) projectGroupRefs.current.set(group.id, element);
                      else projectGroupRefs.current.delete(group.id);
                    }}
                    className={cn('app-project-group', draggingProjectId === group.id && 'is-dragging')}
                    onDragOver={(event) => handleProjectDragOver(event, group.id)}
                    onDrop={(event) => event.preventDefault()}
                  >
                    <button
                      type="button"
                      className={cn('app-project-row', selectedProject && 'is-active')}
                      draggable
                      onDragStart={(event) => handleProjectDragStart(event, group.id)}
                      onDragEnd={handleProjectDragEnd}
                      onClick={(event) => {
                        if (projectDragMovedRef.current) {
                          event.preventDefault();
                          return;
                        }
                        setSelectedProjectId(group.id);
                        toggleProjectCollapsed(group.id);
                      }}
                      title={group.path}
                    >
                      <ChevronRight
                        className={cn('h-3.5 w-3.5 shrink-0 transition-transform', !group.isCollapsed && 'rotate-90')}
                      />
                      <FolderOpen className="h-4 w-4 shrink-0" />
                      <span className="app-project-name">{group.label}</span>
                      <span className="app-project-count">{group.conversations.length}</span>
                    </button>
                    {!group.isCollapsed && (
                      <div className="app-session-list">
                        {visible.items.map((c) => {
                          const isSelected = c.id === currentConversation?.id;
                          const isRunning = activeStreamConversationIds.has(c.id);
                          return (
                            <div
                              key={c.id}
                              className={cn('app-session-row', isSelected && 'is-active')}
                              onClick={() => handleSelectConversation(c.id)}
                              onMouseEnter={() => setHoveredId(c.id)}
                              onMouseLeave={() => setHoveredId(null)}
                              title={c.title || '未命名'}
                            >
                              <span className="app-session-title">{c.title || '未命名'}</span>
                              {isRunning && (
                                <Loader2
                                  className="app-session-running h-3.5 w-3.5"
                                  aria-label="正在运行"
                                />
                              )}
                              <span className="app-session-time">{formatConversationTime(c.updated_at)}</span>
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className={cn(
                                      'app-session-more',
                                      hoveredId === c.id || isSelected ? 'opacity-100' : 'opacity-0'
                                    )}
                                    onClick={(e) => e.stopPropagation()}
                                  >
                                    <MoreHorizontal className="h-4 w-4" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent>
                                  <DropdownMenuItem onClick={() => handleRenameClick(c.id, c.title)}>
                                    <Pencil className="h-4 w-4 mr-2" />
                                    重命名
                                  </DropdownMenuItem>
                                  <DropdownMenuItem onClick={() => deleteConversation(c.id)}>
                                    <X className="h-4 w-4 mr-2" />
                                    删除对话
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            </div>
                          );
                        })}
                        {visible.canExpand && (
                          <button
                            type="button"
                            className="app-sidebar-inline-action"
                            onClick={() => toggleHistoryExpanded(group.id)}
                          >
                            展开 {visible.hiddenCount} 个更多会话
                          </button>
                        )}
                        {visible.canCollapse && (
                          <button
                            type="button"
                            className="app-sidebar-inline-action"
                            onClick={() => toggleHistoryExpanded(group.id)}
                          >
                            收起
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}

        <div className="app-sidebar-footer">
          <button
            type="button"
            className="app-sidebar-action"
            onClick={() => openSettings('providers')}
            title="设置"
          >
            <Settings className="h-4 w-4 shrink-0" />
            {!sidebarCollapsed && <span>设置</span>}
          </button>
        </div>
      </nav>

      {/* Center: title bar + content (chat or tree) */}
      <section className="flex-1 flex flex-col overflow-hidden relative" style={{ background: 'var(--bg-surface)' }}>
        {/* Title bar with view toggle */}
        <div
          className="flex items-center justify-between p-3 sticky top-0 z-[1] min-h-[56px]"
          style={{ background: 'var(--bg-surface)', borderBottom: '0.5px solid var(--border)' }}
        >
          <span className="w-8" />
          <div className="flex items-center gap-2">
            <span className="font-semibold" style={{ color: 'var(--fg-secondary)' }}>{currentConversation?.title || '请选择对话'}</span>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 w-8 p-0"
                  onClick={toggleChatViewMode}
                >
                  {chatViewMode === 'chat' ? (
                    <Network className="h-4 w-4" />
                  ) : (
                    <MessageSquare className="h-4 w-4" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {chatViewMode === 'chat' ? '切换到树视图' : '切换到对话视图'}
              </TooltipContent>
            </Tooltip>
          </div>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-8 w-8 p-0"
              onClick={handleExportMarkdown}
              disabled={!messages.length}
              title="导出为 Markdown"
            >
              <Download className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {/* Chat view */}
        {chatViewMode === 'chat' && (
          !currentConversation && messages.length === 0 ? (
            <div className="new-chat-stage">
              <div className="new-chat-center">
                <h1 className="new-chat-title">我们应该在 ChatTree 中构建什么？</h1>
                <div className="new-chat-composer-wrap">
                  <ChatInput
                    variant="composer"
                    settingsSlot={projectSettingsSlot}
                    onSend={handleSend}
                    onStop={handleStopStreaming}
                    isStreaming={isStreaming}
                    disabled={isStreaming}
                    conversationId={null}
                    editValue={editValue}
                    onEditValueConsumed={() => setEditValue(null)}
                    attachedFiles={attachedFiles}
                    attachedImages={attachedImageRefs.map(ref => ({
                      filename: ref.filename,
                      url: getImportAssetUrl(ref.filename),
                    }))}
                    onFilesPicked={handleFilesPicked}
                    onRemoveFile={handleRemoveFile}
                    onPreviewImage={handlePreviewImage}
                    queuedMessages={visibleQueuedMessages}
                    onUpdateQueuedMessage={handleUpdateQueuedMessage}
                    onDeleteQueuedMessage={handleDeleteQueuedMessage}
                  />
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                ref={historyRef}
                className={cn(
                  'w-full flex-1 overflow-y-scroll pt-4 pb-[140px] flex flex-col items-center custom-scrollbar',
                  isScrolling && 'scrollbar-visible'
                )}
                onScroll={handleScroll}
              >
                <div className="w-[800px] max-w-full flex flex-col px-4">
                  {messages.map((m, index) => renderMsg(m, index))}
                  {showPendingBubble && (
                    <div className="w-full my-2 flex flex-col items-end">
                      <div className="flex flex-col items-start max-w-full">
                        <div
                          className="max-w-full w-fit px-3 py-2 rounded-2xl rounded-br-sm leading-relaxed prose prose-sm prose-invert max-w-none [&_p]:m-0"
                          style={{
                            background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
                            border: '0.5px solid rgba(217,119,87,0.28)',
                            boxShadow: 'var(--highlight-top)',
                            color: 'var(--fg-85)',
                            fontSize: 'var(--codex-chat-font-size)',
                            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                          }}
                        >
                          <MarkdownView content={pendingUserMessage} />
                        </div>
                      </div>
                    </div>
                  )}
                  {showStreamBlock && (
                    <div className="w-full my-2 flex flex-col items-start">
                      <div className="flex flex-col items-start max-w-full">
                        {streamedTimeline.map((block, blockIndex) => {
                          if (block.type === 'reasoning') {
                            const reasoningStillOpen = blockIndex === streamedActiveReasoningIndex;
                            return <ThinkingBlock key={block.key} reasoning={block.reasoning} streaming={reasoningStillOpen} />;
                          }
                          if (block.type === 'tools') {
                            return <ToolCallGroup key={block.key} items={block.items} />;
                          }
                          return (
                            <div
                              key={block.key}
                              className="max-w-full w-fit px-3 py-2 rounded-2xl rounded-bl-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
                              style={{
                                color: 'var(--fg-secondary)',
                                fontSize: 'var(--codex-chat-font-size)',
                                lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                              }}
                            >
                              <MarkdownView content={block.content} />
                            </div>
                          );
                        })}
                        <ToolApprovalGroup approvals={pendingApprovalList} />
                        {streamedTimeline.length === 0 && pendingApprovalList.length === 0 && streamStatus === 'streaming' && (
                          <div
                            className="max-w-full w-fit px-3 py-2 rounded-2xl rounded-bl-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
                            style={{
                              color: 'var(--fg-secondary)',
                              fontSize: 'var(--codex-chat-font-size)',
                              lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                            }}
                          >
                            <div className="flex items-center gap-2">
                              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
                              <span className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>思考中...</span>
                            </div>
                          </div>
                        )}
                        <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
                          <span>{formatDuration(streamDuration)}</span>
                          {getStreamStatusText() && (
                            <span className="text-destructive">{getStreamStatusText()}</span>
                          )}
                        </div>
                      </div>
                    </div>
                  )}
                  <div ref={messagesEndRef} />
                </div>
              </div>
              <div
                aria-hidden="true"
                className="pointer-events-none absolute bottom-0 left-0 right-0 z-[9] h-[150px]"
                style={{
                  background: 'linear-gradient(180deg, color-mix(in srgb, var(--bg-surface) 0%, transparent), var(--bg-surface) 72%)',
                }}
              />
              <footer className="absolute bottom-4 left-1/2 -translate-x-1/2 w-[800px] max-w-[calc(100%-48px)] z-10">
                <ChatInput
                  onSend={handleSend}
                  onStop={handleStopStreaming}
                  isStreaming={isStreaming}
                  disabled={isStreaming}
                  conversationId={currentConversation?.id || null}
                  editValue={editValue}
                  onEditValueConsumed={() => setEditValue(null)}
                  attachedFiles={attachedFiles}
                  attachedImages={attachedImageRefs.map(ref => ({
                    filename: ref.filename,
                    url: getImportAssetUrl(ref.filename),
                  }))}
                  onFilesPicked={handleFilesPicked}
                  onRemoveFile={handleRemoveFile}
                  onPreviewImage={handlePreviewImage}
                  queuedMessages={visibleQueuedMessages}
                  onUpdateQueuedMessage={handleUpdateQueuedMessage}
                  onDeleteQueuedMessage={handleDeleteQueuedMessage}
                />
              </footer>
            </>
          )
        )}

        {/* Tree view */}
        {chatViewMode === 'tree' && (
          <div className="flex-1 overflow-hidden">
            <TreeView />
          </div>
        )}
      </section>

      {/* Right outline (only in chat mode, collapsible) */}
      {chatViewMode === 'chat' && (
        <aside
          className="flex flex-col transition-[width] duration-200 overflow-y-auto overflow-x-hidden custom-scrollbar"
          style={{
            width: outlineCollapsed ? '56px' : '280px',
            background: 'var(--bg-surface)',
            borderLeft: '0.5px solid var(--border)',
          }}
        >
          <div className="flex justify-between items-center p-3 sticky top-0 z-[1] min-h-[56px]"
               style={{ background: 'var(--bg-surface)' }}>
            {!outlineCollapsed && <span className="font-semibold" style={{ color: 'var(--fg-secondary)' }}>大纲</span>}
            <Button
              variant="ghost"
              size="sm"
              className="app-panel-toggle"
              onClick={() => setOutlineCollapsed(!outlineCollapsed)}
              title={outlineCollapsed ? '展开大纲' : '收起大纲'}
            >
              {outlineCollapsed ? <PanelRightOpen className="h-5 w-5" /> : <PanelRightClose className="h-5 w-5" />}
            </Button>
          </div>

          {!outlineCollapsed && outline.map((item, idx) => (
            <div
              key={idx}
              className="flex items-center py-2 px-3 cursor-pointer rounded-lg mx-2 my-0.5 transition-colors"
              style={{ color: 'var(--fg-85)' }}
              title={item.text}
              onClick={() => handleJumpToMessage(item.originalIndex)}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
            >
              <span className="truncate text-sm">{item.text}</span>
            </div>
          ))}
        </aside>
      )}

      {/* Rename dialog */}
      <Dialog open={renameDialogOpen} onOpenChange={(open) => !open && handleRenameCancel()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重命名对话</DialogTitle>
          </DialogHeader>
          <Input
            value={renameTitle}
            onChange={(e) => setRenameTitle(e.target.value)}
            placeholder="请输入新标题"
            onKeyDown={(e) => e.key === 'Enter' && handleRenameConfirm()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={handleRenameCancel}>取消</Button>
            <Button onClick={handleRenameConfirm}>确认</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Project folder dialog */}
      <Dialog open={projectFolderDialogMode !== null} onOpenChange={(open) => { if (!open) closeProjectFolderDialog(); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {projectFolderDialogMode === 'create' ? '新建文件夹' : '使用现有文件夹'}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
                文件夹路径
              </label>
              <Input
                value={projectFolderPath}
                onChange={(event) => setProjectFolderPath(event.target.value)}
                placeholder="D:\\Projects\\ChatTree"
                disabled={projectFolderSubmitting}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') handleProjectFolderSubmit();
                }}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
                项目名称
              </label>
              <Input
                value={projectFolderLabel}
                onChange={(event) => setProjectFolderLabel(event.target.value)}
                placeholder="留空则使用文件夹名"
                disabled={projectFolderSubmitting}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') handleProjectFolderSubmit();
                }}
              />
            </div>
            {projectFolderError && (
              <div className="rounded-md px-3 py-2 text-xs" style={{
                color: 'var(--destructive)',
                background: 'color-mix(in srgb, var(--destructive) 10%, transparent)',
                border: '0.5px solid color-mix(in srgb, var(--destructive) 28%, transparent)',
              }}>
                {projectFolderError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeProjectFolderDialog} disabled={projectFolderSubmitting}>取消</Button>
            <Button onClick={handleProjectFolderSubmit} disabled={projectFolderSubmitting}>
              {projectFolderSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
              确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* File preview dialog */}
      <Dialog open={!!previewFile} onOpenChange={(open) => { if (!open) setPreviewFile(null); }}>
        <DialogContent
          className="max-w-[92vw] sm:max-w-[92vw] max-h-[86vh] flex flex-col"
          style={{ width: 'min(1120px, 92vw)' }}
        >
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {previewFile?.name}
            </DialogTitle>
          </DialogHeader>
          <div className="file-preview-panel">
            {previewFile && (
              <FilePreviewCode name={previewFile.name} content={previewFile.content} />
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Image preview dialog */}
      <Dialog open={!!previewImage} onOpenChange={(open) => { if (!open) setPreviewImage(null); }}>
        <DialogContent className="max-w-[92vw] max-h-[92vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="truncate">{previewImage?.name}</DialogTitle>
          </DialogHeader>
          <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto rounded-md"
               style={{ background: 'var(--bg-button-tertiary-hover)' }}>
            {previewImage && (
              <img
                src={previewImage.url}
                alt={previewImage.name}
                className="max-h-[78vh] max-w-full object-contain"
              />
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}










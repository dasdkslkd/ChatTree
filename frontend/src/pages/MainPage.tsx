import {
  lazy,
  Suspense,
  useEffect,
  useState,
  useRef,
  useLayoutEffect,
  useCallback,
  useMemo,
  isValidElement,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
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
import { TextTooltip } from '@/components/ui/text-tooltip';
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
  Plus, X, MoreHorizontal, ChevronRight, Square,
  Copy, Check, Pencil, Loader2, Network, MessageSquare, FileText, Download, FolderOpen, FolderPlus, Search, Settings,
  PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, ArrowLeft,
} from 'lucide-react';
import { conversationApi } from '../api/conversation';
import { messageApi, type ActiveStreamInfo, type ToolResultSlice } from '../api/message';
import { runsApi } from '../api/runs';
import { plansService } from '../services/plans';
import { transcriptService } from '../services/transcript';
import type {
  Message,
  SendMessageRequest,
  ToolPermissionMode,
} from '../types/message';
import type { PlanSession } from '../types/plan';
import type { TranscriptItem } from '../types/transcript';
import type { MultiAgentMode, WorkspaceContext } from '../types/conversation';
import { useConversationStore } from '../store/conversationStore';
import { useModelStore } from '../store/modelStore';
import { useNavigationStore } from '../store/navigationStore';
import { useRunManager } from '../hooks/useRunManager';
import { streamManager, type StreamState } from '../services/streamManager';
import { slashRegistry } from '../services/slashRegistry';
import { getStreamStatusText as getStreamStatusLabel } from '../utils/generationStatus';
import {
  isDetachedRunView,
  isRunBlockingSelectedBranch,
  isRunStoppableFromSelectedBranch,
  isRunVisibleInSelectedTranscript,
  isRunVisibleInMainTranscript,
  shouldPatchRunIntoMainConversation,
} from '../utils/runVisibility';
import { resolveSendNodeId, resolveSlashStreamNodeId, shouldSendSlashAnchorNode } from '../utils/sendTarget';
import { getSlashRunLabel, shouldQueueForMainThread, shouldRenderRunDraft } from '../utils/slashRuntime';
import {
  groupDetachedSideRuns,
  getWorkflowProgressSteps,
  type SideRunGroupItem,
} from '../utils/sideRunGrouping';
import {
  SIDE_RUN_KINDS,
  getVisibleSideRunRecords,
  isCommandRunStatus,
} from '../utils/sideRunSync';
import { collectSideRunNotifications } from '../utils/sideRunNotifications';
import {
  isTaskNotificationMessage,
  shouldExportMessage,
} from '../utils/taskNotificationVisibility';
import {
  DEFAULT_TOOL_PERMISSION_MODE,
  createToolPermissionDraft,
  syncToolPermissionDraftFromBranch,
  type ToolPermissionDraft,
} from '../utils/toolPermissionDraft';
import {
  collectPendingToolApprovalPrompts,
  type ToolApprovalDecisionHandler,
} from '../utils/toolApprovals';
import {
  extractToolResultEnvelope,
  formatToolArguments,
  formatToolOutput,
  isToolResultError,
  summarizeToolCall,
  type ToolResultEnvelope,
} from '../utils/toolDisplay';
import {
  formatProcessedDuration,
  getAssistantFoldedContentBlocks,
  getStreamingTimelineFoldState,
  type StreamingTimelineFoldState,
} from '../utils/assistantTimelineFolding';
import { createLiveAssistantProcessItem } from '../utils/assistantTimeline';
import {
  getActiveStreamPollingDelay,
  getConversationActiveStreamLookupLimit,
} from '../utils/activeStreamPolling';
import { ChatInput } from '../components/ChatInput';
import { TranscriptList } from '../components/transcript/TranscriptList';
import TreeView from './TreeView';
import {
  getVisibleProjectConversations,
  getWorkspaceForNewConversation,
  groupConversationsByProject,
  encodeProjectId,
} from '../utils/projectGroups';
import {
  LEFT_SIDEBAR_WIDTH,
  LEFT_SIDEBAR_WIDTH_STORAGE_KEY,
  RIGHT_PANEL_WIDTH,
  RIGHT_PANEL_WIDTH_STORAGE_KEY,
  getKeyboardResizedSidebarWidth,
  getPointerResizedSidebarWidth,
  readStoredSidebarWidth,
  writeStoredSidebarWidth,
  type SidebarResizeSide,
  type SidebarWidthConfig,
} from '../utils/sidebarResize';
import {
  mergeLiveRunTranscriptItems,
  normalizeTranscriptItems,
  type LiveRunTranscriptOverlay,
} from '../utils/transcriptItems';

const MarkdownContent = lazy(() => import('../components/MarkdownContent'));
const MANUAL_PROJECTS_STORAGE_KEY = 'chattree.manualProjectWorkspaces';
const PROJECT_ORDER_STORAGE_KEY = 'chattree.projectOrder';
const PLAN_MODE_TOOL_NAMES = new Set(['enter_plan_mode', 'update_plan', 'exit_plan_mode', 'ask_user_question']);

type SidebarResizeSession = {
  side: SidebarResizeSide;
  startClientX: number;
  startWidth: number;
  storageKey: string;
  config: SidebarWidthConfig;
};

function getBrowserStorage(): Storage | null {
  return typeof window === 'undefined' ? null : window.localStorage;
}

function getTranscriptRequestKey(conversationId: string, nodeId?: string | null): string {
  return `${conversationId}:${nodeId || ''}`;
}

function getTranscriptItemNodeId(item: TranscriptItem): string | null {
  return item.node_id || item.anchor_node_id || null;
}

function getEditableUserMessageParentNodeId(item: TranscriptItem, messages: Message[]): string | null {
  const nodeId = getTranscriptItemNodeId(item);
  if (!nodeId) return null;
  const messageParentNodeId = messages.find((message) =>
    message.node_id === nodeId && message.role === 'user'
  )?.parent_node_id;
  if (messageParentNodeId) return messageParentNodeId;
  const propsParentNodeId = item.props?.parent_node_id;
  return typeof propsParentNodeId === 'string' && propsParentNodeId ? propsParentNodeId : null;
}

function getEditableUserMessageAttachmentRefs(
  item: TranscriptItem,
  messages: Message[],
): {
  importFiles: string[];
  imageRefs: Array<{ filename: string; mime_type?: string }>;
} {
  const nodeId = getTranscriptItemNodeId(item);
  const message = nodeId
    ? messages.find((candidate) => candidate.node_id === nodeId && candidate.role === 'user')
    : null;
  return {
    importFiles: (message?.import_files ?? []).map((file) => file.filename).filter(Boolean),
    imageRefs: (message?.image_refs ?? []).filter((file) => Boolean(file.filename)),
  };
}

function messageReferencesAttachment(message: Message, filename: string): boolean {
  return Boolean(
    message.import_files?.some((file) => file.filename === filename)
    || message.image_refs?.some((file) => file.filename === filename)
  );
}

function isTranscriptItemVisibleNow(
  item: TranscriptItem,
  currentConversationId: string | null,
  selectedBranchTipId: string | null,
): boolean {
  if (!currentConversationId) return false;
  if (item.conversation_id && item.conversation_id !== currentConversationId) return false;
  const itemNodeId = getTranscriptItemNodeId(item);
  return !itemNodeId || itemNodeId === selectedBranchTipId;
}

function isTranscriptItemOnCurrentBranch(
  item: TranscriptItem,
  currentConversationId: string | null,
  currentBranchNodeIds: Set<string>,
): boolean {
  if (!currentConversationId) return false;
  if (item.conversation_id && item.conversation_id !== currentConversationId) return false;
  const itemNodeId = getTranscriptItemNodeId(item);
  return Boolean(itemNodeId && currentBranchNodeIds.has(itemNodeId));
}

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

function normalizeCodeLanguage(language: string): string {
  const normalized = language.toLowerCase();
  if (normalized === 'js') return 'javascript';
  if (normalized === 'ts') return 'typescript';
  if (normalized === 'py') return 'python';
  if (normalized === 'ps1' || normalized === 'pwsh' || normalized === 'shell') return 'powershell';
  if (normalized === 'sh' || normalized === 'zsh') return 'bash';
  if (normalized === 'yml') return 'yaml';
  return normalized;
}

function getCodeBlockPayload(children: React.ReactNode): { code: string; language: string | null } | null {
  const codeElement = Array.isArray(children)
    ? children.find((child) => isValidElement(child))
    : children;
  if (!isValidElement(codeElement)) return null;
  const props = codeElement.props as { className?: string; children?: React.ReactNode };
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

function CodeBlockWrapper({ children, ...props }: React.HTMLAttributes<HTMLPreElement>) {
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

type SideRunDraft = {
  run: StreamState;
  showPendingBubble: boolean;
  showStreamBlock: boolean;
  timeline: AssistantTimelineBlock[];
  streamingFoldState: StreamingTimelineFoldState<AssistantTimelineBlock>;
  activeReasoningIndex: number;
  activeReasoningKey: string | null;
};

type QueuedMessage = {
  id: string;
  conversationId: string;
  nodeId?: string | null;
  anchorNodeId?: string | null;
  blockingRunIds?: string[];
  content: string;
  request: SendMessageRequest;
};

function createQueuedMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `queued-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function getStreamGenerationStatus(status: StreamState['status']): 'completed' | 'error' | 'stopped' {
  if (status === 'error') return 'error';
  if (status === 'stopped') return 'stopped';
  return 'completed';
}

function createAssistantMessageFromStream(run: StreamState): Message | null {
  if (!shouldPatchRunIntoMainConversation(run)) return null;
  const nodeId = run.targetNodeId || run.nodeId;
  if (!nodeId) return null;
  const status = getStreamGenerationStatus(run.status);
  return {
    id: `stream-${run.runId}-${nodeId}`,
    role: 'assistant',
    content: run.content,
    node_id: nodeId,
    parent_node_id: run.anchorNodeId || undefined,
    timestamp: Date.now() / 1000,
    tokens_used: run.tokensUsed || undefined,
    generation_info: {
      duration_ms: run.duration,
      status,
      error_message: run.errorMessage,
      tokens_used: run.tokensUsed || undefined,
    },
    reasoning: run.reasoning || undefined,
    tool_interactions: run.toolInteractions.length > 0 ? run.toolInteractions : undefined,
  };
}

function scheduleIdleTask(task: () => void, timeout = 1200): () => void {
  const win = window as typeof window & {
    requestIdleCallback?: (callback: () => void, options?: { timeout?: number }) => number;
    cancelIdleCallback?: (handle: number) => void;
  };
  if (typeof win.requestIdleCallback === 'function') {
    const handle = win.requestIdleCallback(task, { timeout });
    return () => win.cancelIdleCallback?.(handle);
  }
  const handle = window.setTimeout(task, Math.min(timeout, 600));
  return () => window.clearTimeout(handle);
}

function normalizeToolPermissionMode(value: unknown): ToolPermissionMode | undefined {
  return value === 'auto_approve' || value === 'modify_only' || value === 'ask_always' || value === 'plan'
    ? value
    : undefined;
}

function getBranchToolPermissionMode(
  messages: Array<{ node_id?: string | null; tool_permission_mode?: ToolPermissionMode | null }>,
  nodeId: string | null,
): ToolPermissionMode | undefined {
  if (!nodeId) return undefined;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.node_id === nodeId) {
      const mode = normalizeToolPermissionMode(message.tool_permission_mode);
      if (mode) return mode;
    }
  }
  return undefined;
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

function getSideRunAssistantTimeline(message: {
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

function createLiveRunTranscriptItem(run: StreamState): TranscriptItem {
  return createLiveAssistantProcessItem(run);
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

function AnimatedProcessedBlocks({
  expanded,
  blocks,
  renderBlock,
}: {
  expanded: boolean;
  blocks: AssistantTimelineBlock[];
  renderBlock: (block: AssistantTimelineBlock) => React.ReactNode;
}) {
  const [rendered, setRendered] = useState(expanded);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let frame = 0;
    let timer = 0;
    if (expanded) {
      setRendered(true);
      frame = window.requestAnimationFrame(() => setVisible(true));
    } else {
      setVisible(false);
      timer = window.setTimeout(() => setRendered(false), 180);
    }
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      if (timer) window.clearTimeout(timer);
    };
  }, [expanded]);

  if (!rendered) return null;

  return (
    <div
      className={cn('processed-blocks-shell', visible && 'expanded')}
      aria-hidden={!visible}
    >
      <div className="processed-blocks-inner">
        {blocks.map(renderBlock)}
      </div>
    </div>
  );
}

/* ---------- Component ---------- */
export default function ChatPage() {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [rightPanelView, setRightPanelView] = useState<'outline' | 'side'>('outline');
  const [selectedSideRunId, setSelectedSideRunId] = useState<string | null>(null);
  const [leftSidebarWidth, setLeftSidebarWidth] = useState(() =>
    readStoredSidebarWidth(getBrowserStorage(), LEFT_SIDEBAR_WIDTH_STORAGE_KEY, LEFT_SIDEBAR_WIDTH),
  );
  const [rightPanelWidth, setRightPanelWidth] = useState(() =>
    readStoredSidebarWidth(getBrowserStorage(), RIGHT_PANEL_WIDTH_STORAGE_KEY, RIGHT_PANEL_WIDTH),
  );
  const [resizingSidebar, setResizingSidebar] = useState<SidebarResizeSide | null>(null);
  const [scrollPositions, setScrollPositions] = useState<Record<string, number>>({});
  const [isScrolling, setIsScrolling] = useState(false);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [editValue, setEditValue] = useState<string | null>(null);
  const [editTargetNodeId, setEditTargetNodeId] = useState<string | null>(null);
  const [editToolPermissionMode, setEditToolPermissionMode] = useState<ToolPermissionMode | null>(null);
  const [editReturnNodeId, setEditReturnNodeId] = useState<string | null>(null);
  const [editProtectedAttachmentNames, setEditProtectedAttachmentNames] = useState<string[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<string[]>([]);
  const [attachedImageRefs, setAttachedImageRefs] = useState<Array<{ filename: string; mime_type?: string }>>([]);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [hiddenSideRunIdsByConversation, setHiddenSideRunIdsByConversation] = useState<Record<string, string[]>>({});
  const [transcriptItems, setTranscriptItems] = useState<TranscriptItem[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [, setCopiedTranscriptRunId] = useState<string | null>(null);
  const handledSideRunNotificationsRef = useRef<Set<string>>(new Set());
  const [toolPermissionDraft, setToolPermissionDraftState] = useState<ToolPermissionDraft>(() => createToolPermissionDraft());
  const [newConversationMultiAgentMode, setNewConversationMultiAgentMode] = useState<MultiAgentMode>('explicit_request_only');
  const [previewImage, setPreviewImage] = useState<{ name: string; url: string } | null>(null);
  const [conversationSearch, setConversationSearch] = useState('');
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
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
  const sidebarResizeRef = useRef<SidebarResizeSession | null>(null);

  const userScrollingRef = useRef(false);
  const scrollEndTimeoutRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  const toolPermissionDraftRef = useRef<ToolPermissionDraft>(toolPermissionDraft);
  const transcriptRequestTokensRef = useRef<Map<string, symbol>>(new Map());

  const beginSidebarResize = useCallback((
    event: ReactPointerEvent<HTMLDivElement>,
    side: SidebarResizeSide,
  ) => {
    if (event.button !== 0) return;

    const session = side === 'left'
      ? {
          side,
          startClientX: event.clientX,
          startWidth: leftSidebarWidth,
          storageKey: LEFT_SIDEBAR_WIDTH_STORAGE_KEY,
          config: LEFT_SIDEBAR_WIDTH,
        }
      : {
          side,
          startClientX: event.clientX,
          startWidth: rightPanelWidth,
          storageKey: RIGHT_PANEL_WIDTH_STORAGE_KEY,
          config: RIGHT_PANEL_WIDTH,
        };

    sidebarResizeRef.current = session;
    setResizingSidebar(side);
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  }, [leftSidebarWidth, rightPanelWidth]);

  const adjustSidebarWidthFromKeyboard = useCallback((
    event: ReactKeyboardEvent<HTMLDivElement>,
    side: SidebarResizeSide,
  ) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;

    event.preventDefault();

    if (side === 'left') {
      const nextWidth = getKeyboardResizedSidebarWidth(side, event.key, leftSidebarWidth, LEFT_SIDEBAR_WIDTH);
      setLeftSidebarWidth(writeStoredSidebarWidth(
        getBrowserStorage(),
        LEFT_SIDEBAR_WIDTH_STORAGE_KEY,
        nextWidth,
        LEFT_SIDEBAR_WIDTH,
      ));
      return;
    }

    const nextWidth = getKeyboardResizedSidebarWidth(side, event.key, rightPanelWidth, RIGHT_PANEL_WIDTH);
    setRightPanelWidth(writeStoredSidebarWidth(
      getBrowserStorage(),
      RIGHT_PANEL_WIDTH_STORAGE_KEY,
      nextWidth,
      RIGHT_PANEL_WIDTH,
    ));
  }, [leftSidebarWidth, rightPanelWidth]);

  useEffect(() => {
    if (!resizingSidebar) return;

    const applyWidth = (session: SidebarResizeSession, width: number) => {
      const storedWidth = writeStoredSidebarWidth(getBrowserStorage(), session.storageKey, width, session.config);
      if (session.side === 'left') {
        setLeftSidebarWidth(storedWidth);
      } else {
        setRightPanelWidth(storedWidth);
      }
    };

    const handlePointerMove = (event: PointerEvent) => {
      const session = sidebarResizeRef.current;
      if (!session) return;

      applyWidth(session, getPointerResizedSidebarWidth(
        session.side,
        session.startWidth,
        session.startClientX,
        event.clientX,
        session.config,
      ));
    };

    const finishResize = () => {
      sidebarResizeRef.current = null;
      setResizingSidebar(null);
    };

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finishResize);
    window.addEventListener('pointercancel', finishResize);

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finishResize);
      window.removeEventListener('pointercancel', finishResize);
    };
  }, [resizingSidebar]);

  const { chatViewMode, toggleChatViewMode, openSettings } = useNavigationStore();

  const updateToolPermissionDraft = useCallback((draft: ToolPermissionDraft) => {
    toolPermissionDraftRef.current = draft;
    setToolPermissionDraftState(draft);
  }, []);

  const getToolPermissionDraft = useCallback(() => toolPermissionDraftRef.current, []);

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
    currentNodeId, pendingScrollNodeId, clearPendingScroll,
    createConversation, selectConversation, deleteConversation, deleteNode, switchNode, loadConversations,
    clearCurrentConversation, updateConversationTitle, refreshMessages, patchAssistantMessageFromStream,
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

  const activeRunStates = useRunManager(currentConversation?.id ?? null);
  const [activePlan, setActivePlan] = useState<PlanSession | null>(null);
  const [planActionPending, setPlanActionPending] = useState<'approve' | 'reject' | 'answer' | null>(null);
  const [planRejectFeedback, setPlanRejectFeedback] = useState('');
  const [planError, setPlanError] = useState<string | null>(null);
  const currentConversationIdRef = useRef<string | null>(null);
  const currentVisibleTranscriptKeyRef = useRef<string | null>(null);
  currentConversationIdRef.current = currentConversation?.id ?? null;
  const refreshTranscript = useCallback(async (
    conversationId: string | null | undefined,
    nodeId?: string | null,
  ) => {
    if (!conversationId) {
      setTranscriptItems([]);
      setTranscriptError(null);
      setTranscriptLoading(false);
      return;
    }

    const requestKey = getTranscriptRequestKey(conversationId, nodeId);
    const requestToken = Symbol(requestKey);
    transcriptRequestTokensRef.current.set(requestKey, requestToken);
    const isCurrentVisibleRequest = () => requestKey === currentVisibleTranscriptKeyRef.current;
    if (isCurrentVisibleRequest()) {
      setTranscriptLoading(true);
      setTranscriptError(null);
    }

    try {
      const items = await transcriptService.fetchTranscript(conversationId, nodeId);
      if (transcriptRequestTokensRef.current.get(requestKey) !== requestToken) return;
      if (!isCurrentVisibleRequest()) return;
      setTranscriptItems(normalizeTranscriptItems(items));
      setTranscriptError(null);
    } catch (_) {
      if (transcriptRequestTokensRef.current.get(requestKey) !== requestToken) return;
      if (isCurrentVisibleRequest()) {
        setTranscriptError('对话 transcript 刷新失败，已保留当前内容');
      }
    } finally {
      if (transcriptRequestTokensRef.current.get(requestKey) === requestToken) {
        transcriptRequestTokensRef.current.delete(requestKey);
        if (isCurrentVisibleRequest()) {
          setTranscriptLoading(false);
        }
      }
    }
  }, []);
  const hiddenSideRunIds = useMemo(() => {
    const conversationId = currentConversation?.id;
    return new Set(conversationId ? hiddenSideRunIdsByConversation[conversationId] ?? [] : []);
  }, [currentConversation?.id, hiddenSideRunIdsByConversation]);
  const sidePanelRunStates = useMemo(
    () => activeRunStates.filter((run) => !hiddenSideRunIds.has(run.runId)),
    [activeRunStates, hiddenSideRunIds],
  );

  const hideSideRun = useCallback((conversationId: string, runId: string) => {
    setHiddenSideRunIdsByConversation((current) => {
      const existing = current[conversationId] ?? [];
      if (existing.includes(runId)) return current;
      return {
        ...current,
        [conversationId]: [...existing, runId],
      };
    });
  }, []);
  useEffect(() => {
    void slashRegistry.refresh().catch(() => {});
  }, []);
  const startStreaming = useCallback(
    async (
      convId: string,
      request: SendMessageRequest,
      pending: string | null = null,
      requestNodeId?: string,
      anchorNodeId?: string | null,
    ) => {
      await streamManager.startStream(convId, request, pending, requestNodeId, anchorNodeId);
    },
    [],
  );
  const refreshActivePlan = useCallback(async (conversationId: string | null | undefined) => {
    if (!conversationId) {
      if (!currentConversationIdRef.current) setActivePlan(null);
      return;
    }
    if (conversationId !== currentConversationIdRef.current) return;
    try {
      const plan = await plansService.fetchActive(conversationId);
      if (conversationId !== currentConversationIdRef.current) return;
      setActivePlan(plan);
      setPlanError(null);
    } catch (_) {
      if (conversationId === currentConversationIdRef.current) setActivePlan(null);
    }
  }, []);
  useEffect(() => {
    setActivePlan(null);
    setPlanActionPending(null);
    setPlanError(null);
    setPlanRejectFeedback('');
  }, [currentConversation?.id]);
  const [localStreamingConversationCounts, setLocalStreamingConversationCounts] = useState<Map<string, number>>(() => new Map());
  const [backendActiveStreamConversationCounts, setBackendActiveStreamConversationCounts] = useState<Map<string, number>>(() => new Map());
  const activeStreamConversationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const [id, count] of localStreamingConversationCounts) counts.set(id, Math.max(counts.get(id) ?? 0, count));
    for (const [id, count] of backendActiveStreamConversationCounts) counts.set(id, Math.max(counts.get(id) ?? 0, count));
    return counts;
  }, [localStreamingConversationCounts, backendActiveStreamConversationCounts]);
  const projectDragMovedRef = useRef(false);
  const projectGroupRefs = useRef(new Map<string, HTMLDivElement>());
  const projectFlipFirstRef = useRef<Map<string, number> | null>(null);

  // 结构性去重：一旦本轮流式产生的节点已出现在真实消息里（refreshMessages 注入），
  // 就隐藏对应的乐观叠加层，无论 cleanup 何时执行。这样真实消息与乐观叠加层
  // 永远不会同时渲染同一轮，杜绝“重复两轮”。
  // 注意：后端在流式 START 时就已创建节点并保存 user 消息，但 assistant 消息要到
  // 结束才保存。因此必须按角色分别判断——否则中途重新进入正在流式的对话会
  // 把 user 消息拉回 messages，误判“整轮已落地”而把正在生成的助手块也隐藏掉。
  const currentBranchNodeIds = useMemo(
    () => new Set(messages.map((message) => message.node_id).filter(Boolean)),
    [messages],
  );
  const selectedBranchTipId = currentNodeId || currentConversation?.current_node_id || null;
  currentVisibleTranscriptKeyRef.current = currentConversation?.id
    ? getTranscriptRequestKey(currentConversation.id, selectedBranchTipId)
    : null;
  const currentBranchToolPermissionMode = useMemo(
    () => getBranchToolPermissionMode(messages, selectedBranchTipId),
    [messages, selectedBranchTipId],
  );
  const liveBranchToolPermissionMode = useMemo(() => {
    const runs = activeRunStates
      .filter((run) => isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds))
      .filter((run) => normalizeToolPermissionMode(run.toolPermissionMode))
      .sort((a, b) => b.createdAt - a.createdAt);
    return normalizeToolPermissionMode(runs[0]?.toolPermissionMode);
  }, [activeRunStates, currentBranchNodeIds, selectedBranchTipId]);
  const activePlanToolSignal = useMemo(() => activeRunStates
    .map((run) => {
      const toolNames: string[] = [];
      for (const interaction of run.toolInteractions || []) {
        for (const call of interaction?.assistant?.tool_calls || []) {
          const name = call?.function?.name;
          if (PLAN_MODE_TOOL_NAMES.has(name)) toolNames.push(name);
        }
        for (const tool of interaction?.tools || []) {
          const name = tool?.name;
          if (PLAN_MODE_TOOL_NAMES.has(name)) toolNames.push(name);
        }
      }
      return toolNames.length > 0 ? `${run.runId}:${run.eventCount}:${toolNames.join(',')}` : '';
    })
    .filter(Boolean)
    .join('|'), [activeRunStates]);

  useEffect(() => {
    const next = syncToolPermissionDraftFromBranch(
      toolPermissionDraftRef.current,
      liveBranchToolPermissionMode ?? currentBranchToolPermissionMode,
    );
    if (next !== toolPermissionDraftRef.current) {
      updateToolPermissionDraft(next);
    }
  }, [currentBranchToolPermissionMode, liveBranchToolPermissionMode, updateToolPermissionDraft]);

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId || !activePlanToolSignal) return;
    void refreshActivePlan(conversationId);
  }, [activePlanToolSignal, currentConversation?.id, refreshActivePlan]);

  useEffect(() => {
    if (currentConversation || toolPermissionDraftRef.current.explicit) return;
    const current = toolPermissionDraftRef.current;
    if (current.mode !== DEFAULT_TOOL_PERMISSION_MODE) {
      updateToolPermissionDraft(createToolPermissionDraft());
    }
  }, [currentConversation, updateToolPermissionDraft]);

  useEffect(() => {
    const activeKeys = new Set<string>();
    for (const run of activeRunStates) {
      for (const notification of collectSideRunNotifications(run.toolInteractions, run.sideRunNotifications)) {
        activeKeys.add(`${run.conversationId}:${notification.runId}`);
      }
    }
    for (const key of handledSideRunNotificationsRef.current) {
      if (!activeKeys.has(key)) handledSideRunNotificationsRef.current.delete(key);
    }
  }, [activeRunStates]);

  const sideRunDrafts = useMemo(() => sidePanelRunStates
    .filter((run) => shouldRenderRunDraft(run))
    .map((run) => {
      if (!isDetachedRunView(run, selectedBranchTipId, currentBranchNodeIds)) return null;
      const timeline = getSideRunAssistantTimeline({
        content: run.content,
        reasoning: run.reasoning,
        tool_interactions: run.toolInteractions,
      });
      const streamingFoldedContentBlocks = getAssistantFoldedContentBlocks({
        content: run.content,
        reasoning: run.reasoning,
        tool_interactions: run.toolInteractions,
      });
      const streamingFoldState = getStreamingTimelineFoldState(
        timeline,
        streamingFoldedContentBlocks.map((block) => block.key),
      );
      let activeReasoningIndex = -1;
      let activeReasoningKey: string | null = null;
      if (run.status === 'streaming') {
        for (let i = timeline.length - 1; i >= 0; i -= 1) {
          if (timeline[i].type === 'reasoning') {
            const hasLaterBlock = timeline.slice(i + 1).some((block) => block.type !== 'reasoning');
            activeReasoningIndex = run.reasoningActive || !hasLaterBlock ? i : -1;
            activeReasoningKey = activeReasoningIndex >= 0 ? timeline[activeReasoningIndex].key : null;
            break;
          }
        }
      }
      return {
        run,
        showPendingBubble: !!run.pendingUserMessage,
        showStreamBlock: run.status !== 'idle',
        timeline,
        streamingFoldState,
        activeReasoningIndex,
        activeReasoningKey,
      };
    })
    .filter((draft): draft is SideRunDraft => Boolean(draft))
    .filter((draft) => draft.showPendingBubble || draft.showStreamBlock),
    [currentBranchNodeIds, selectedBranchTipId, sidePanelRunStates],
  );

  const sideRunGroups = useMemo(
    () => groupDetachedSideRuns(sideRunDrafts),
    [sideRunDrafts],
  );
  const sideRunTopLevelCount = useMemo(
    () => sideRunGroups.reduce((total, group) => total + group.runs.length, 0),
    [sideRunGroups],
  );
  const selectedSideRunItem = useMemo((): SideRunGroupItem<SideRunDraft> | null => {
    if (!selectedSideRunId) return null;
    for (const group of sideRunGroups) {
      for (const item of group.runs) {
        if (item.run.runId === selectedSideRunId) return item;
        const step = item.steps.find((candidate) => candidate.run.runId === selectedSideRunId);
        if (step) {
          return {
            draft: step,
            run: step.run,
            steps: [],
          };
        }
      }
    }
    return null;
  }, [selectedSideRunId, sideRunGroups]);

  const sideRunActivity = useMemo(
    () => sideRunDrafts.map((draft) => [
      draft.run.runId,
      draft.run.status,
      draft.run.content.length,
      draft.run.reasoning.length,
      draft.run.toolInteractions.length,
      Object.keys(draft.run.pendingApprovals).length,
      draft.run.pendingUserMessage?.length ?? 0,
    ].join(':')).join('|'),
    [sideRunDrafts],
  );
  const currentBranchStreamingRunIds = useMemo(
    () => activeRunStates
      .filter((run) => isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => run.runId),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const currentBranchStoppableRunIds = useMemo(
    () => activeRunStates
      .filter((run) => isRunStoppableFromSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => run.runId),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const currentBranchHasStreamingChat = currentBranchStreamingRunIds.length > 0;
  const currentBranchStreamActivity = useMemo(
    () => activeRunStates
      .filter((run) => isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => [
      run.runId,
      run.status,
      run.content.length,
      run.reasoning.length,
      run.toolInteractions.length,
      Object.keys(run.pendingApprovals).length,
      run.pendingUserMessage?.length ?? 0,
    ].join(':')).join('|'),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const currentBranchHasPendingUserMessage = useMemo(
    () => activeRunStates.some((run) =>
      shouldRenderRunDraft(run)
      && Boolean(run.pendingUserMessage)
      && isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds)
    ),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const liveMainTranscriptRunOverlays = useMemo<LiveRunTranscriptOverlay[]>(
    () => activeRunStates
      .filter((run) => run.status !== 'completed')
      .filter((run) => shouldRenderRunDraft(run))
      .filter((run) => isRunVisibleInMainTranscript(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => ({
        runId: run.runId,
        nodeId: run.nodeId,
        targetNodeId: run.targetNodeId,
        anchorNodeId: run.anchorNodeId,
        items: [createLiveRunTranscriptItem(run)],
      }))
      .filter((overlay) => overlay.items.length > 0),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const displayTranscriptItems = useMemo(() => {
    const merged = mergeLiveRunTranscriptItems(transcriptItems, liveMainTranscriptRunOverlays);
    const planText = typeof activePlan?.plan === 'string' ? activePlan.plan : '';
    if (activePlan?.status !== 'awaiting_approval' || !planText.trim()) return merged;
    const planRecord = activePlan as PlanSession & {
      submitted_node_id?: string | null;
      entered_node_id?: string | null;
      proposal_id?: string | null;
      proposal_revision?: number | null;
    };
    const activePlanId = activePlan.plan_id || activePlan.id || '';
    if (!activePlanId) return merged;
    const nodeId = planRecord.submitted_node_id || planRecord.entered_node_id || null;
    const card: TranscriptItem = {
      id: `active-plan-${activePlanId}`,
      type: 'plan_card',
      conversation_id: activePlan.conversation_id || currentConversation?.id || null,
      node_id: nodeId,
      plan_id: activePlanId,
      status: 'awaiting_approval',
      preview: planText,
      visibility: 'main',
      props: {
        plan: planText,
        status: 'awaiting_approval',
        proposal_id: planRecord.proposal_id || null,
        revision: planRecord.proposal_revision || null,
      },
    };
    let insertionIndex = 0;
    if (nodeId) {
      for (let index = merged.length - 1; index >= 0; index -= 1) {
        if (merged[index].node_id === nodeId || merged[index].anchor_node_id === nodeId) {
          insertionIndex = index + 1;
          break;
        }
      }
    }
    if (insertionIndex > 0) {
      return [
        ...merged.slice(0, insertionIndex),
        card,
        ...merged.slice(insertionIndex),
      ];
    }
    return [...merged, card];
  }, [activePlan, currentConversation?.id, liveMainTranscriptRunOverlays, transcriptItems]);
  const approvalPromptRunStates = sidePanelRunStates;
  const pendingToolApprovalPrompts = useMemo(
    () => collectPendingToolApprovalPrompts(approvalPromptRunStates),
    [approvalPromptRunStates],
  );
  const pendingApprovalCount = pendingToolApprovalPrompts.length;

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) {
      setTranscriptItems([]);
      return;
    }
    void refreshTranscript(conversationId, selectedBranchTipId);
  }, [currentConversation?.id, currentBranchStreamActivity, refreshTranscript, selectedBranchTipId]);

  useEffect(() => {
    if (sideRunTopLevelCount === 0) return;
    setOutlineCollapsed(false);
    setRightPanelView('side');
  }, [sideRunActivity, sideRunTopLevelCount]);

  useEffect(() => {
    if (selectedSideRunId && !selectedSideRunItem) {
      setSelectedSideRunId(null);
    }
  }, [selectedSideRunId, selectedSideRunItem]);

  const visibleQueuedMessages = useMemo(
    () => queuedMessages
      .filter((message) =>
        message.conversationId === currentConversation?.id
        && (!message.nodeId || message.nodeId === selectedBranchTipId)
      )
      .map(({ id, content }) => ({ id, content })),
    [queuedMessages, currentConversation?.id, selectedBranchTipId],
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
  const newChatProjectLabel = selectedNewConversationWorkspace.label || '默认项目';
  const filteredProjectGroups = projectPickerSearch.trim()
    ? allProjectGroups.filter((group) => {
        const query = projectPickerSearch.trim().toLowerCase();
        return `${group.label} ${group.path}`.toLowerCase().includes(query);
      })
    : allProjectGroups;
  const handleProjectPickerOpenChange = (open: boolean) => {
    setProjectPickerOpen(open);
    if (!open) setProjectPickerSearch('');
  };

  const projectSettingsSlot = (
    <DropdownMenu open={projectPickerOpen} onOpenChange={handleProjectPickerOpenChange}>
      <TextTooltip content={selectedProjectGroup?.path || selectedNewConversationWorkspace.cwd || '默认项目'}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="new-chat-setting-chip"
            aria-label="选择项目"
          >
            <FolderOpen className="h-4 w-4" />
            <span className="truncate">{selectedProjectGroup?.label || selectedNewConversationWorkspace.label || '默认项目'}</span>
            <ChevronRight className="h-3.5 w-3.5 rotate-90 opacity-70" />
          </button>
        </DropdownMenuTrigger>
      </TextTooltip>
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
            <TextTooltip key={group.id} content={group.path} side="right">
              <button
                type="button"
                className="new-chat-project-option"
                onClick={() => {
                  setSelectedProjectId(group.id);
                  setProjectPickerSearch('');
                  setProjectPickerOpen(false);
                }}
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
            </TextTooltip>
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
            onClick={() => {
              setProjectPickerOpen(false);
              openProjectFolderDialog('create');
            }}
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
            onClick={() => {
              setProjectPickerOpen(false);
              openProjectFolderDialog('existing');
            }}
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
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    clearCurrentConversation();
  };
  const sendNextQueuedMessage = useCallback(async (conversationId: string): Promise<boolean> => {
    const nextMessage = queuedMessagesRef.current.find((message) =>
      message.conversationId === conversationId && message.content.trim()
      && (
        !message.blockingRunIds?.length
        || streamManager.areRunsInactive(message.blockingRunIds)
      )
    );
    if (!nextMessage) {
      updateQueuedMessages((messages) =>
        messages.filter((message) => message.conversationId !== conversationId || message.content.trim())
      );
      return false;
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
      nextMessage.nodeId || undefined,
      nextMessage.anchorNodeId ?? nextMessage.nodeId ?? null,
    );
    return true;
  }, [startStreaming, updateQueuedMessages]);

  const handleUpdateQueuedMessage = useCallback((id: string, content: string) => {
    updateQueuedMessages((messages) =>
      messages.map((message) => message.id === id ? { ...message, content } : message)
    );
  }, [updateQueuedMessages]);

  const handleDeleteQueuedMessage = useCallback((id: string) => {
    updateQueuedMessages((messages) => messages.filter((message) => message.id !== id));
  }, [updateQueuedMessages]);

  const syncBackendScheduledFollowup = useCallback(async (conversationId: string) => {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      try {
        const activeStreams = await messageApi.getActiveStreams(conversationId);
        const attachable = activeStreams.filter((item) =>
          !item.done
          && (item.node_id || item.run_id)
          && !(item.run_id && item.kind && SIDE_RUN_KINDS.has(item.kind) && hiddenSideRunIds.has(item.run_id))
        );
        if (attachable.length > 0) {
          await Promise.all(attachable
            .filter((active) => active.node_id)
            .map((active) => refreshMessages(conversationId, {
              awaitNodeId: active.node_id ?? undefined,
              awaitRole: 'user',
              retries: 0,
            })));
          for (const active of attachable) {
            void streamManager.resumeStream(
              conversationId,
              active.node_id ?? null,
              active.run_id ?? undefined,
              0,
              active.anchor_node_id ?? null,
              active.kind ?? 'chat',
            );
          }
          return;
        }
        await refreshMessages(conversationId, { retries: 0 });
      } catch (_) {
        await refreshMessages(conversationId, { retries: 0 });
      }
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    await loadConversations();
  }, [hiddenSideRunIds, loadConversations, refreshMessages]);

  const syncSelectedConversationSideRuns = useCallback(async (conversationId: string) => {
    const runs = await runsApi.listConversation(conversationId);
    const sideRuns = getVisibleSideRunRecords(runs, hiddenSideRunIds);
    for (const run of sideRuns) {
      if (streamManager.hasRun(run.run_id)) continue;
      if (!isCommandRunStatus(run.status)) {
        void streamManager.resumeStream(
          conversationId,
          run.target_node_id ?? null,
          run.run_id,
          0,
          run.anchor_node_id ?? null,
          run.kind,
        );
        continue;
      }
      const events = await runsApi.events(run.run_id, 0);
      if (hiddenSideRunIds.has(run.run_id)) continue;
      streamManager.restoreRunFromEvents(run, events);
    }
  }, [hiddenSideRunIds]);

  useEffect(() => {
    for (const run of activeRunStates) {
      const notifications = collectSideRunNotifications(run.toolInteractions, run.sideRunNotifications);
      if (notifications.length === 0) continue;
      const unseen = notifications.filter((notification) => {
        const key = `${run.conversationId}:${notification.runId}`;
        if (handledSideRunNotificationsRef.current.has(key)) return false;
        return !hiddenSideRunIds.has(notification.runId);
      });
      if (unseen.length > 0) {
        void syncSelectedConversationSideRuns(run.conversationId).then(() => {
          for (const notification of unseen) {
            handledSideRunNotificationsRef.current.add(`${run.conversationId}:${notification.runId}`);
          }
        });
      }
    }
  }, [activeRunStates, hiddenSideRunIds, syncSelectedConversationSideRuns]);

  const handleToolApprovalDecision = useCallback<ToolApprovalDecisionHandler>(async (
    approvalId,
    decision,
    scope,
    runId,
  ) => {
    await messageApi.decideApproval(approvalId, decision, scope);
    const run = activeRunStates.find((item) => item.runId === runId);
    const conversationId = run?.conversationId ?? currentConversation?.id ?? null;
    if (!conversationId) return;
    void streamManager.resumeStream(
      conversationId,
      run?.targetNodeId ?? run?.nodeId ?? null,
      runId,
      run?.eventCount ?? 0,
      run?.anchorNodeId ?? null,
      run?.kind ?? 'chat',
    );
    await refreshMessages(conversationId, { retries: 0 });
    await refreshTranscript(conversationId, run?.targetNodeId ?? run?.nodeId ?? selectedBranchTipId);
  }, [activeRunStates, currentConversation?.id, refreshMessages, refreshTranscript, selectedBranchTipId]);

  const handleCopyTranscriptItem = useCallback(async (_item: TranscriptItem, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedTranscriptRunId(_item.id);
      window.setTimeout(() => setCopiedTranscriptRunId(null), 1600);
    } catch (error) {
      console.error('Failed to copy transcript item:', error);
    }
  }, []);

  const handleEditUserMessage = useCallback(async (item: TranscriptItem, text: string) => {
    if (!isTranscriptItemOnCurrentBranch(item, currentConversation?.id ?? null, currentBranchNodeIds)) return;
    const parentNodeId = getEditableUserMessageParentNodeId(item, messages);
    if (!parentNodeId) return;
    const inheritedToolPermissionMode = liveBranchToolPermissionMode ?? currentBranchToolPermissionMode ?? null;
    const attachmentRefs = getEditableUserMessageAttachmentRefs(item, messages);
    const protectedAttachmentNames = [
      ...attachmentRefs.importFiles,
      ...attachmentRefs.imageRefs.map((file) => file.filename),
    ];
    setEditValue(text);
    setEditTargetNodeId(parentNodeId);
    setEditToolPermissionMode(inheritedToolPermissionMode);
    setEditReturnNodeId(selectedBranchTipId);
    setEditProtectedAttachmentNames(protectedAttachmentNames);
    setAttachedFiles(attachmentRefs.importFiles);
    setAttachedImageRefs(attachmentRefs.imageRefs);
    await switchNode(parentNodeId);
  }, [
    currentBranchNodeIds,
    currentBranchToolPermissionMode,
    currentConversation?.id,
    liveBranchToolPermissionMode,
    messages,
    selectedBranchTipId,
    switchNode,
  ]);

  const handleCancelEdit = useCallback(async () => {
    const returnNodeId = editReturnNodeId;
    const conversationId = currentConversation?.id;
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    setAttachedFiles([]);
    setAttachedImageRefs([]);
    if (conversationId && returnNodeId) {
      await switchNode(returnNodeId);
    }
  }, [currentConversation?.id, editReturnNodeId, switchNode]);

  const handleDeleteUserMessage = useCallback(async (item: TranscriptItem) => {
    if (!isTranscriptItemVisibleNow(item, currentConversation?.id ?? null, selectedBranchTipId)) return;
    const nodeId = getTranscriptItemNodeId(item);
    if (!nodeId || !currentConversation?.id) return;
    if (!window.confirm('确定删除这条消息及其后续分支？')) return;
    await deleteNode(nodeId);
    await refreshTranscript(currentConversation.id);
  }, [currentConversation?.id, deleteNode, refreshTranscript, selectedBranchTipId]);

  // Legacy static coverage still keys off the historical marker:
  // const handleApprovePlan = useCallback(async () => {
  const handleApprovePlan = useCallback(async (item: TranscriptItem) => {
    if (!isTranscriptItemVisibleNow(item, currentConversation?.id ?? null, selectedBranchTipId)) return;
    const conversationId = item.conversation_id || currentConversation?.id;
    const planId = item.plan_id || '';
    const actionNodeId = getTranscriptItemNodeId(item) || selectedBranchTipId;
    if (!conversationId) return;
    if (!planId) return;
    setPlanActionPending('approve');
    setPlanError(null);
    try {
      setActivePlan((current) => {
        if (!current) return current;
        const currentPlanId = current.plan_id || current.id || '';
        return currentPlanId === planId ? null : current;
      });
      const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
      setShouldAutoScroll(true);
      await streamManager.startPlanApprovalStream(
        conversationId,
        planId,
        {
          reasoning_effort: currentReasoningEffort,
          thinking_enabled: currentThinkingEnabled,
        },
        actionNodeId,
      );
      await refreshActivePlan(conversationId);
      await refreshTranscript(conversationId, actionNodeId);
    } catch (error) {
      console.error('Failed to approve plan:', error);
      setPlanError('批准失败，请稍后重试');
    } finally {
      setPlanActionPending(null);
    }
  }, [currentConversation?.id, refreshActivePlan, refreshTranscript, selectedBranchTipId]);

  const handleRejectPlan = useCallback(async (item: TranscriptItem) => {
    if (!isTranscriptItemVisibleNow(item, currentConversation?.id ?? null, selectedBranchTipId)) return;
    const feedback = planRejectFeedback.trim() || '请修改计划。';
    const conversationId = item.conversation_id || currentConversation?.id;
    const planId = item.plan_id || '';
    const actionNodeId = getTranscriptItemNodeId(item) || selectedBranchTipId;
    if (!conversationId) return;
    if (!planId) return;
    setPlanActionPending('reject');
    setPlanError(null);
    try {
      setActivePlan((current) => {
        if (!current) return current;
        const currentPlanId = current.plan_id || current.id || '';
        return currentPlanId === planId ? null : current;
      });
      const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
      setShouldAutoScroll(true);
      setPlanRejectFeedback('');
      await streamManager.startPlanRejectStream(
        conversationId,
        planId,
        {
          feedback,
          reasoning_effort: currentReasoningEffort,
          thinking_enabled: currentThinkingEnabled,
        },
        actionNodeId,
      );
      await refreshActivePlan(conversationId);
      await refreshTranscript(conversationId, actionNodeId);
    } catch (error) {
      console.error('Failed to reject plan:', error);
      setPlanError('提交修改意见失败，请稍后重试');
    } finally {
      setPlanActionPending(null);
    }
  }, [currentConversation?.id, planRejectFeedback, refreshActivePlan, refreshTranscript, selectedBranchTipId]);

  const handleAnswerPlanQuestion = useCallback(async (item: TranscriptItem, answerOverride?: string) => {
    if (!isTranscriptItemVisibleNow(item, currentConversation?.id ?? null, selectedBranchTipId)) return;
    const answer = (answerOverride ?? '').trim();
    if (!answer) return;
    const conversationId = item.conversation_id || currentConversation?.id;
    const planId = item.plan_id || '';
    const actionNodeId = getTranscriptItemNodeId(item) || selectedBranchTipId;
    if (!conversationId) return;
    if (!planId) return;
    setPlanActionPending('answer');
    setPlanError(null);
    try {
      setActivePlan((current) => {
        if (!current) return current;
        const currentPlanId = current.plan_id || current.id || '';
        return currentPlanId === planId
          ? { ...current, status: 'active', question: { ...(current.question || {}), answer } }
          : current;
      });
      const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
      setShouldAutoScroll(true);
      void streamManager.startPlanAnswerStream(
        conversationId,
        planId,
        answer,
        {
          reasoning_effort: currentReasoningEffort,
          thinking_enabled: currentThinkingEnabled,
        },
        actionNodeId,
      ).then(async () => {
        await refreshActivePlan(conversationId);
        await refreshTranscript(conversationId, actionNodeId);
      }).catch((error) => {
        console.error('Failed to answer plan question:', error);
        setPlanError('提交回答失败，请稍后重试');
      });
    } catch (error) {
      console.error('Failed to answer plan question:', error);
      setPlanError('提交回答失败，请稍后重试');
    } finally {
      setPlanActionPending(null);
    }
  }, [currentConversation?.id, refreshActivePlan, refreshTranscript, selectedBranchTipId]);

  const handleStopStreaming = useCallback(() => {
    if (currentConversation?.id) {
      const conversationId = currentConversation.id;
      updateQueuedMessages((messages) => messages.filter((message) =>
        message.conversationId !== conversationId
        || (
          message.nodeId != null
          && message.nodeId !== selectedBranchTipId
        )
      ));
    }
    const conversationId = currentConversation?.id;
    void (async () => {
      if (conversationId) {
        const localStops = currentBranchStoppableRunIds.map((runId) => streamManager.stopRun(runId));
        await Promise.allSettled(localStops);
        setBackendActiveStreamConversationCounts((current) => {
          const activeCount = streamManager.getConversationStates(conversationId)
            .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval' || state.status === 'stopping')
            .length;
          const next = new Map(current);
          if (activeCount > 0) next.set(conversationId, activeCount);
          else next.delete(conversationId);
          return next;
        });
        await refreshMessages(conversationId, { retries: 1 });
        await refreshTranscript(conversationId, selectedBranchTipId);
      }
    })();
  }, [currentBranchStoppableRunIds, currentConversation?.id, refreshMessages, refreshTranscript, selectedBranchTipId, updateQueuedMessages]);

  // 全局注册一次：任意对话的流结束（completed/error/stopped）时，
  // 从后端刷新真实消息，再清理 StreamManager 中该对话的临时状态。
  // 不依赖当前查看的是哪个对话，因此切走的对话流完成也能正确落地。
  useEffect(() => {
    const unsubscribe = streamManager.onFinish(async ({ conversationId: finishedId, runId, drained, nodeId, targetNodeId, controller }) => {
      const finishedRun = streamManager.getConversationStates(finishedId).find((state) => state.runId === runId);
      const shouldPatchMainConversation = finishedRun ? shouldPatchRunIntoMainConversation(finishedRun) : true;
      const streamMessage = shouldPatchMainConversation && finishedRun ? createAssistantMessageFromStream(finishedRun) : null;
      const patchedAssistant = streamMessage
        ? patchAssistantMessageFromStream(finishedId, streamMessage, finishedRun?.pendingUserMessage)
        : false;
      const awaitNodeId = shouldPatchMainConversation
        ? targetNodeId ?? nodeId ?? streamMessage?.node_id ?? undefined
        : undefined;
      const hasQueuedFollowup = queuedMessagesRef.current.some((message) =>
        message.conversationId === finishedId
        && message.content.trim()
      );

      if (!shouldPatchMainConversation || (!targetNodeId && !nodeId)) {
        if (!finishedRun || !shouldRenderRunDraft(finishedRun)) {
          streamManager.cleanupIfController(finishedId, controller, runId);
        }
        await loadConversations();
        await refreshTranscript(finishedId, awaitNodeId ?? null);
        void syncSelectedConversationSideRuns(finishedId);
        const sentQueued = await sendNextQueuedMessage(finishedId);
        if (!sentQueued) void syncBackendScheduledFollowup(finishedId);
        return;
      }

      if (patchedAssistant && !hasQueuedFollowup) {
        streamManager.cleanupIfController(finishedId, controller, runId);
        scheduleIdleTask(() => {
          void (async () => {
            await refreshMessages(
              finishedId,
              drained
                ? (awaitNodeId ? { awaitNodeId, retries: 0 } : undefined)
                : { awaitNodeId, retries: 6 },
            );
            await refreshTranscript(finishedId, awaitNodeId ?? null);
            await loadConversations();
            void syncSelectedConversationSideRuns(finishedId);
            void syncBackendScheduledFollowup(finishedId);
          })();
        });
        return;
      }

      // 完成判据：等待本轮节点(nodeId)的 assistant 消息落盘，而非“消息数 +1”。
      // 对多消息轮次（未来工具轮次）同样稳健。nodeId 为空（停得太早还没拿到）时
      // refreshMessages 退化为单次拉取。
      // drained=true：后端在 [DONE] 前已保存，一次即可拿到最终结果。
      // drained=false（硬 abort）：保存由连接断开触发，与刷新竞态，需轮询重试，
      //   期间保留乐观气泡，避免“用户消息瞬间消失”。
      const confirmed = await refreshMessages(
        finishedId,
        drained
          ? (awaitNodeId ? { awaitNodeId, retries: 0 } : undefined)
          : { awaitNodeId, retries: 6 },
      );
      await refreshTranscript(finishedId, awaitNodeId ?? null);
      // 仅当确认真实消息已落地，才清理临时流状态（移除乐观气泡）。
      // 身份校验：若 await 期间用户对同一对话发起了新流，controller 已被替换则跳过。
      if (drained || confirmed) {
        streamManager.cleanupIfController(finishedId, controller, runId);
      } else {
        // 硬 abort 且后端保存超过重试预算：保留乐观气泡，延后再确认一次，
        // 成功后再清理，彻底避免用户消息闪失。
        setTimeout(async () => {
          await refreshMessages(finishedId, { awaitNodeId, retries: 6 });
          await refreshTranscript(finishedId, awaitNodeId ?? null);
          // 无论是否确认，这是最后兜底：清理临时状态，避免气泡永久残留。
          streamManager.cleanupIfController(finishedId, controller, runId);
        }, 800);
      }
      // 同步对话列表（更新时间、标题等）
      await loadConversations();
      void syncSelectedConversationSideRuns(finishedId);
      const sentQueued = await sendNextQueuedMessage(finishedId);
      if (!sentQueued) void syncBackendScheduledFollowup(finishedId);
    });
    return unsubscribe;
  }, [
    refreshMessages,
    patchAssistantMessageFromStream,
    loadConversations,
    refreshTranscript,
    syncSelectedConversationSideRuns,
    sendNextQueuedMessage,
    syncBackendScheduledFollowup,
  ]);

  const shouldAutoScrollRef = useRef(shouldAutoScroll);
  shouldAutoScrollRef.current = shouldAutoScroll;

  useEffect(() => {
    if (currentBranchHasStreamingChat && shouldAutoScrollRef.current && !userScrollingRef.current) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [currentBranchStreamActivity, currentBranchHasStreamingChat, scrollToBottom]);

  useEffect(() => {
    if (currentBranchHasPendingUserMessage) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [currentBranchHasPendingUserMessage, scrollToBottom]);

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
      const counts = new Map<string, number>();
      for (const conversationId of streamManager.getStreamingConversationIds()) {
        const count = streamManager.getConversationStates(conversationId)
          .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval')
          .length;
        if (count > 0) counts.set(conversationId, count);
      }
      setLocalStreamingConversationCounts(counts);
    };
    updateLocalStreamingIds();
    return streamManager.subscribe(updateLocalStreamingIds);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const scheduleNextSync = (activeStreamCount: number) => {
      clearTimer();
      const delay = getActiveStreamPollingDelay({
        activeStreamCount,
        documentHidden: document.hidden,
      });
      if (delay === null || cancelled) return;
      timer = window.setTimeout(syncBackendActiveStreams, delay);
    };

    const syncBackendActiveStreams = async () => {
      if (document.hidden) {
        scheduleNextSync(0);
        return;
      }
      try {
        const activeStreams = await messageApi.getAllActiveStreams();
        if (cancelled) return;
        const counts = new Map<string, number>();
        for (const item of activeStreams) {
          if (!item.conversation_id) continue;
          counts.set(item.conversation_id, (counts.get(item.conversation_id) ?? 0) + 1);
        }
        setBackendActiveStreamConversationCounts(counts);
        scheduleNextSync(activeStreams.filter((item) => !item.done).length);
      } catch (_) {
        if (!cancelled) setBackendActiveStreamConversationCounts(new Map());
        scheduleNextSync(0);
      }
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearTimer();
        return;
      }
      void syncBackendActiveStreams();
    };

    void syncBackendActiveStreams();
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      cancelled = true;
      clearTimer();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  const currentBackendActiveStreamHintCount = currentConversation?.id
    ? backendActiveStreamConversationCounts.get(currentConversation.id) ?? 0
    : 0;

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) {
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        let activeStreams: ActiveStreamInfo[] = [];
        const maxAttempts = getConversationActiveStreamLookupLimit({
          activeStreamHintCount: currentBackendActiveStreamHintCount,
        });
        for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
          activeStreams = await messageApi.getActiveStreams(conversationId);
          if (cancelled) return;
          if (activeStreams.some((item) =>
            !item.done
            && (item.node_id || item.run_id)
            && !(item.run_id && item.kind && SIDE_RUN_KINDS.has(item.kind) && hiddenSideRunIds.has(item.run_id))
          )) break;
          if (attempt + 1 >= maxAttempts) break;
          await new Promise((resolve) => window.setTimeout(resolve, 500));
        }
        if (cancelled) return;
        const attachable = activeStreams.filter((item) =>
          !item.done
          && (item.node_id || item.run_id)
          && !(item.run_id && item.kind && SIDE_RUN_KINDS.has(item.kind) && hiddenSideRunIds.has(item.run_id))
        );
        if (attachable.length === 0) {
          return;
        }
        await Promise.all(attachable
          .filter((active) => active.node_id)
          .map((active) => refreshMessages(conversationId, {
            awaitNodeId: active.node_id ?? undefined,
            awaitRole: 'user',
            retries: 0,
          })));
        if (cancelled) return;
        for (const active of attachable) {
          void streamManager.resumeStream(
            conversationId,
            active.node_id ?? null,
            active.run_id ?? undefined,
            0,
            active.anchor_node_id ?? null,
            active.kind ?? 'chat',
          );
        }
      } catch (_) {
        await refreshMessages(conversationId, { retries: 0 });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [currentBackendActiveStreamHintCount, currentConversation?.id, hiddenSideRunIds, refreshMessages]);

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) return;
    void syncSelectedConversationSideRuns(conversationId).catch(() => {
      // Side run history is best-effort UI state; active stream recovery above still handles live runs.
    });
  }, [currentConversation?.id, syncSelectedConversationSideRuns]);

  const latestMessageId = messages.length > 0 ? messages[messages.length - 1]?.id : null;
  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) {
      setActivePlan(null);
      return;
    }
    void refreshActivePlan(conversationId);
  }, [currentConversation?.id, latestMessageId, refreshActivePlan]);

  const handleSelectConversation = async (id: string) => {
    if (currentConversation && historyRef.current) {
      setScrollPositions(prev => ({
        ...prev,
        [currentConversation.id]: historyRef.current!.scrollTop
      }));
    }
    pendingScrollId.current = id;
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
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
    for (const m of messages.filter(shouldExportMessage)) {
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
        multi_agent_mode: newConversationMultiAgentMode,
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
    const isReferencedByHistory = messages.some((message) => messageReferencesAttachment(message, filename));
    const isProtectedEditAttachment = editProtectedAttachmentNames.includes(filename);
    try {
      if (!isReferencedByHistory && !isProtectedEditAttachment) {
        await conversationApi.deleteImport(currentConversation.id, filename);
      }
    } catch (_) {}
    setAttachedFiles(prev => prev.filter(f => f !== filename));
    setAttachedImageRefs(prev => prev.filter(ref => ref.filename !== filename));
    setEditProtectedAttachmentNames(prev => prev.filter(name => name !== filename));
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
    toolPermissionMode?: ToolPermissionMode,
    promptId?: string | null,
    promptMode?: 'override' | 'append',
    multiAgentMode?: MultiAgentMode,
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
      tool_permission_mode: toolPermissionMode ?? (editTargetNodeId ? editToolPermissionMode ?? undefined : undefined),
      import_files: importFiles.length > 0 ? importFiles : undefined,
      image_refs: imageRefs.length > 0 ? imageRefs : undefined,
    };
    let sendNodeId = resolveSendNodeId({
      editTargetNodeId,
      currentNodeId,
      conversationCurrentNodeId: currentConversation?.current_node_id,
    });
    const slashMatch = slashRegistry.match(val);
    const slashCommand = slashMatch?.command ?? null;
    let streamNodeId = resolveSlashStreamNodeId({
      sendNodeId,
      streamTargetPolicy: slashCommand?.stream_target_policy,
    });
    if (shouldSendSlashAnchorNode(slashCommand?.stream_target_policy)) {
      request.node_id = sendNodeId ?? undefined;
    }

    if (conversationId && shouldQueueForMainThread({ currentBranchHasStreamingChat, slashCommand })) {
      const queuedConversationId = conversationId;
      clearAttachments();
      setEditTargetNodeId(null);
      setEditToolPermissionMode(null);
      setEditReturnNodeId(null);
      setEditProtectedAttachmentNames([]);
      updateQueuedMessages((messages) => [
        ...messages,
        {
          id: createQueuedMessageId(),
          conversationId: queuedConversationId,
          nodeId: streamNodeId ?? null,
          anchorNodeId: sendNodeId ?? null,
          blockingRunIds: currentBranchStreamingRunIds,
          content: val,
          request,
        },
      ]);
      return;
    }

    if (!conversationId) {
      const newConv = await createConversation({
        title: val.slice(0, 20),
        prompt_id: promptId || undefined,
        prompt_mode: promptId ? promptMode : undefined,
        workspace: workspaceForCreateRequest(),
        multi_agent_mode: multiAgentMode ?? newConversationMultiAgentMode,
      });
      if (!newConv) {
        console.error('Failed to create conversation');
        return;
      }
      conversationId = newConv.id;
      sendNodeId = resolveSendNodeId({
        editTargetNodeId: null,
        currentNodeId: null,
        conversationCurrentNodeId: newConv.current_node_id,
      });
      streamNodeId = resolveSlashStreamNodeId({
        sendNodeId,
        streamTargetPolicy: slashCommand?.stream_target_policy,
      });
      if (shouldSendSlashAnchorNode(slashCommand?.stream_target_policy)) {
        request.node_id = sendNodeId ?? undefined;
      }
    }

    clearAttachments();
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    // 第三个参数是乐观渲染的用户气泡文本（显示用户输入的原文）。
    // 推理设置从 modelStore 的当前值读取（已确认值），随请求透传。
    void startStreaming(
      conversationId,
      request,
      val,
      streamNodeId,
      sendNodeId ?? null,
    ).catch((err) => {
      console.error('发送失败:', err);
    });
  };

  const handleJumpToMessage = (index: number) => {
    const element = document.getElementById(`message-${index}`);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

  const isCompactSummaryMessage = (message: typeof messages[0]) =>
    message.is_compact_summary === true;

  const outline = messages
    .map((m, index) => ({ ...m, originalIndex: index }))
    .filter((m) => m.role === 'user' && !isCompactSummaryMessage(m) && !isTaskNotificationMessage(m))
    .map((m) => {
      const clean = getUserDisplayContent(m);
      return {
        text: clean.slice(0, 20) + (clean.length > 20 ? '...' : ''),
        originalIndex: m.originalIndex,
      };
    });

  const renderAssistantTimelineBlock = (block: AssistantTimelineBlock) => {
    if (block.type === 'reasoning') {
      return <ThinkingBlock key={block.key} reasoning={block.reasoning} />;
    }
    if (block.type === 'tools') {
      return <ToolCallGroup key={block.key} items={block.items} />;
    }
    return (
      <div
        key={block.key}
        className="max-w-full w-full min-w-0 px-3 py-2 rounded-2xl leading-relaxed prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
        style={{
          color: 'var(--fg-secondary)',
          fontSize: 'var(--codex-chat-font-size)',
          lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
        }}
      >
        <MarkdownView content={block.content} enableMermaid />
      </div>
    );
  };

  const renderTranscriptItem = (_item: TranscriptItem, defaultItem: React.ReactNode) => {
    return defaultItem;
  };

  const getSideRunGroupLabel = (kind: string): string => {
    if (kind === 'side_question') return '旁路问题';
    if (kind === 'subagent') return '后台分支';
    if (kind === 'command') return '后台命令';
    if (kind === 'workflow') return 'Workflow';
    if (kind === 'direct_response') return '命令响应';
    return kind;
  };

  const getSideRunTitle = (run: StreamState): string => {
    const metadata = run.metadata || {};
    const candidates = [
      run.summary,
      typeof metadata.command === 'string' ? metadata.command : '',
      typeof metadata.workflow_step_name === 'string' ? metadata.workflow_step_name : '',
      typeof metadata.agent_name === 'string' ? metadata.agent_name : '',
      typeof metadata.original_slash_input === 'string' ? metadata.original_slash_input : '',
      typeof metadata.delegated_task === 'string' ? metadata.delegated_task : '',
      run.pendingUserMessage,
    ];
    return candidates.find((value) => typeof value === 'string' && value.trim().length > 0)?.trim()
      || `${getSlashRunLabel(run.kind, run.pendingUserMessage)} · ${run.runId.slice(0, 12)}`;
  };

  const getSideRunStatusText = (run: StreamState): string => {
    if (run.status === 'streaming') return '运行中';
    if (run.status === 'waiting_approval') return '等待审批';
    if (run.status === 'completed') return '已完成';
    if (run.status === 'error') return '出错';
    if (run.status === 'stopped') return '已停止';
    return run.status;
  };

  const getSideRunStatusColor = (run: StreamState): string => {
    if (run.status === 'completed') return 'var(--fg-tertiary)';
    if (run.status === 'error') return 'var(--destructive)';
    if (run.status === 'stopped') return 'var(--fg-tertiary)';
    if (run.status === 'waiting_approval') return 'var(--icon-accent)';
    return 'var(--icon-accent)';
  };

  const renderSideRunActions = (draft: SideRunDraft) => (
    <>
      {(draft.run.status === 'streaming' || draft.run.status === 'waiting_approval') && (
        <TextTooltip content="停止">
          <button
            type="button"
            className="rounded border-0 bg-transparent p-1"
            onClick={(event) => {
              event.stopPropagation();
              void streamManager.stopRun(draft.run.runId);
            }}
            style={{ color: 'var(--fg-tertiary)' }}
            aria-label="停止"
          >
            <Square className="h-3.5 w-3.5" />
          </button>
        </TextTooltip>
      )}
      <TextTooltip content="关闭">
        <button
          type="button"
          className="rounded border-0 bg-transparent p-1"
          onClick={(event) => {
            event.stopPropagation();
            if (selectedSideRunId === draft.run.runId) setSelectedSideRunId(null);
            if (currentConversation?.id) hideSideRun(currentConversation.id, draft.run.runId);
            streamManager.cleanupRun(draft.run.runId);
          }}
          style={{ color: 'var(--fg-tertiary)' }}
          aria-label="关闭"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </TextTooltip>
    </>
  );

  const renderCommandRunBody = (run: StreamState) => {
    const shell = run.metadata?.shell && typeof run.metadata.shell === 'object'
      ? run.metadata.shell as Record<string, unknown>
      : null;
    const commandLanguage = typeof shell?.highlighter_language === 'string'
      ? shell.highlighter_language
      : 'bash';
    const output = [
      run.command.stdout ? `$ stdout\n${run.command.stdout}` : '',
      run.command.stderr ? `$ stderr\n${run.command.stderr}` : '',
    ].filter(Boolean).join('\n');
    const command = run.command.command
      || (typeof run.metadata.command === 'string' ? run.metadata.command : '')
      || run.summary
      || run.runId;
    const cwd = run.command.cwd || (typeof run.metadata.cwd === 'string' ? run.metadata.cwd : '');
    return (
      <div className="flex flex-col gap-2">
        <div className="rounded border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
          <div className="truncate">{command}</div>
          {cwd && <div className="truncate">cwd: {cwd}</div>}
          {(run.command.exitCode !== null || run.command.durationSeconds !== null) && (
            <div>
              {run.command.exitCode !== null ? `exit: ${run.command.exitCode}` : ''}
              {run.command.durationSeconds !== null ? ` · ${run.command.durationSeconds}s` : ''}
            </div>
          )}
        </div>
        {output ? (
          <div className="file-preview-code-shell custom-scrollbar">
            <SyntaxHighlighter
              language={commandLanguage}
              style={oneDark}
              customStyle={{
                margin: 0,
                background: 'transparent',
                fontSize: '12px',
                lineHeight: '1.45',
              }}
              wrapLongLines
            >
              {output}
            </SyntaxHighlighter>
          </div>
        ) : run.status === 'streaming' ? (
          <div className="flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--fg-tertiary)' }}>
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
            <span>运行中...</span>
          </div>
        ) : null}
        {getStreamStatusLabel(run.status, run.errorMessage) && (
          <div className="text-xs text-destructive">
            {getStreamStatusLabel(run.status, run.errorMessage)}
          </div>
        )}
      </div>
    );
  };

  const renderSideRunBody = (draft: SideRunDraft) => (
    <div className="flex flex-col gap-2">
      {draft.showPendingBubble && (
        <div
          className="max-w-full rounded-lg px-3 py-2 text-sm prose prose-sm prose-invert max-w-none [&_p]:m-0"
          style={{
            background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
            border: '0.5px solid rgba(217,119,87,0.28)',
            color: 'var(--fg-85)',
          }}
        >
          <MarkdownView content={draft.run.pendingUserMessage || ''} />
        </div>
      )}
      {draft.run.kind === 'command' && renderCommandRunBody(draft.run)}
      {draft.showStreamBlock && draft.run.kind !== 'command' && (
        <div className="min-w-0">
          {draft.streamingFoldState.canFoldProcess ? (
            <>
              <div className="processed-fold expanded">
                <div className="processed-fold-button" aria-expanded="true">
                  <span>{draft.run.duration > 0 ? `已处理 ${formatProcessedDuration(draft.run.duration) ?? ''}`.trim() : '已处理'}</span>
                  <ChevronRight className="processed-fold-chevron" />
                </div>
              </div>
              <AnimatedProcessedBlocks
                expanded
                blocks={draft.streamingFoldState.visibleBlocks}
                renderBlock={(block) => {
                  if (block.type === 'reasoning') {
                    return (
                      <ThinkingBlock
                        key={block.key}
                        reasoning={block.reasoning}
                        streaming={block.key === draft.activeReasoningKey}
                      />
                    );
                  }
                  if (block.type === 'tools') {
                    return <ToolCallGroup key={block.key} items={block.items} />;
                  }
                  return renderAssistantTimelineBlock(block);
                }}
              />
            </>
          ) : (
            draft.timeline.map((block, blockIndex) => {
              if (block.type === 'reasoning') {
                const reasoningStillOpen = blockIndex === draft.activeReasoningIndex;
                return <ThinkingBlock key={block.key} reasoning={block.reasoning} streaming={reasoningStillOpen} />;
              }
              if (block.type === 'tools') {
                return <ToolCallGroup key={block.key} items={block.items} />;
              }
              return (
                <div
                  key={block.key}
                  className="max-w-full min-w-0 rounded-lg px-3 py-2 text-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
                  style={{ color: 'var(--fg-secondary)' }}
                >
                  <MarkdownView content={block.content} />
                </div>
              );
            })
          )}
          {draft.timeline.length === 0 && draft.run.status === 'streaming' && (
            <div className="flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--fg-tertiary)' }}>
              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
              <span>运行中...</span>
            </div>
          )}
          {getStreamStatusLabel(draft.run.status, draft.run.errorMessage) && (
            <div className="mt-1 text-xs text-destructive">
              {getStreamStatusLabel(draft.run.status, draft.run.errorMessage)}
            </div>
          )}
        </div>
      )}
    </div>
  );

  const renderWorkflowOverview = (item: SideRunGroupItem<SideRunDraft>) => {
    const progressSteps = getWorkflowProgressSteps(item.run);
    return (
      <div className="flex flex-col gap-3">
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated-secondary, rgba(255,255,255,0.03))' }}>
          <div className="mb-2 text-xs font-semibold" style={{ color: 'var(--fg-secondary)' }}>流程进度</div>
          {progressSteps.length === 0 ? (
            <div className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>等待 workflow 事件。</div>
          ) : (
            <div className="flex flex-col gap-2">
              {progressSteps.map((step) => (
                <div key={step.key} className="flex min-w-0 items-center gap-2 text-sm">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: step.status === 'completed' ? 'var(--fg-tertiary)' : step.status === 'error' ? 'var(--destructive)' : 'var(--icon-accent)' }}
                  />
                  <span className="min-w-0 flex-1 truncate" style={{ color: 'var(--fg-secondary)' }}>{step.label}</span>
                  <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{step.status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated-secondary, rgba(255,255,255,0.03))' }}>
          <div className="mb-2 text-xs font-semibold" style={{ color: 'var(--fg-secondary)' }}>子步骤</div>
          {item.steps.length === 0 ? (
            <div className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>暂无可展开的步骤详情。</div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {item.steps.map((step) => (
                <button
                  key={step.run.runId}
                  type="button"
                  className="flex min-w-0 items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors"
                  style={{ borderColor: 'var(--border)', background: 'transparent', color: 'var(--fg-secondary)' }}
                  onClick={() => setSelectedSideRunId(step.run.runId)}
                >
                  <span className="min-w-0 flex-1 truncate text-sm">{getSideRunTitle(step.run)}</span>
                  <span className="text-xs" style={{ color: getSideRunStatusColor(step.run) }}>{getSideRunStatusText(step.run)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  const activeRightPanelWidth = rightPanelWidth;
  const activeRightPanelConfig = RIGHT_PANEL_WIDTH;

  return (
    <div
      className={cn('flex h-full', resizingSidebar && 'is-sidebar-resizing')}
      style={{ background: 'var(--bg-surface)' }}
    >
      {/* Left conversation list (collapsible) */}
      <nav
        className={cn(
          'app-sidebar',
          sidebarCollapsed && 'app-sidebar-collapsed',
          resizingSidebar === 'left' && 'is-resizing',
        )}
        style={{ width: `${sidebarCollapsed ? 56 : leftSidebarWidth}px` }}
      >
        <div className="app-sidebar-topbar">
          {!sidebarCollapsed && (
            <TextTooltip content="新对话">
              <button
                type="button"
                className={cn('app-new-chat-action', !currentConversation && 'is-active')}
                onClick={handleNewConversation}
              >
                <Plus className="h-4 w-4 shrink-0" />
                <span>新对话</span>
              </button>
            </TextTooltip>
          )}
          <TextTooltip content={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}>
            <Button
              variant="ghost"
              size="sm"
              className="app-panel-toggle"
              onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
              aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
            >
              {sidebarCollapsed ? <PanelLeftOpen className="h-5 w-5" /> : <PanelLeftClose className="h-5 w-5" />}
            </Button>
          </TextTooltip>
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
                    <TextTooltip content={group.path} side="right">
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
                      >
                        <ChevronRight
                          className={cn('h-3.5 w-3.5 shrink-0 transition-transform', !group.isCollapsed && 'rotate-90')}
                        />
                        <FolderOpen className="h-4 w-4 shrink-0" />
                        <span className="app-project-name">{group.label}</span>
                        <span className="app-project-count">{group.conversations.length}</span>
                      </button>
                    </TextTooltip>
                    {!group.isCollapsed && (
                      <div className="app-session-list">
                        {visible.items.map((c) => {
                          const isSelected = c.id === currentConversation?.id;
                          const runningCount = activeStreamConversationCounts.get(c.id) ?? 0;
                          const isRunning = runningCount > 0;
                          return (
                            <TextTooltip key={c.id} content={c.title || '未命名'} side="right">
                              <div
                                className={cn('app-session-row', isSelected && 'is-active')}
                                onClick={() => handleSelectConversation(c.id)}
                                onMouseEnter={() => setHoveredId(c.id)}
                                onMouseLeave={() => setHoveredId(null)}
                              >
                                <span className="app-session-title">{c.title || '未命名'}</span>
                                {isRunning && (
                                  <span className="app-session-running inline-flex items-center gap-1" aria-label={`正在运行 ${runningCount} 个任务`}>
                                    <Loader2 className="h-3.5 w-3.5" />
                                    {runningCount > 1 && <span className="text-[10px] tabular-nums">{runningCount}</span>}
                                  </span>
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
                            </TextTooltip>
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
          <TextTooltip content="设置">
            <button
              type="button"
              className="app-sidebar-action"
              onClick={() => openSettings('providers')}
            >
              <Settings className="h-4 w-4 shrink-0" />
              {!sidebarCollapsed && <span>设置</span>}
            </button>
          </TextTooltip>
        </div>
        {!sidebarCollapsed && (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="调整左侧栏宽度"
            aria-valuemin={LEFT_SIDEBAR_WIDTH.minWidth}
            aria-valuemax={LEFT_SIDEBAR_WIDTH.maxWidth}
            aria-valuenow={leftSidebarWidth}
            tabIndex={0}
            className="sidebar-resize-handle sidebar-resize-handle-left"
            onPointerDown={(event) => beginSidebarResize(event, 'left')}
            onKeyDown={(event) => adjustSidebarWidthFromKeyboard(event, 'left')}
          />
        )}
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
            <TextTooltip content="导出为 Markdown">
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={handleExportMarkdown}
                disabled={!messages.length}
                aria-label="导出为 Markdown"
              >
                <Download className="h-4 w-4" />
              </Button>
            </TextTooltip>
          </div>
        </div>

        {/* Chat view */}
        {chatViewMode === 'chat' && (
          !currentConversation && messages.length === 0 ? (
            <div className="new-chat-stage">
              <div className="new-chat-center">
                <h1 className="new-chat-title">{`我们应该在 ${newChatProjectLabel} 中做些什么？`}</h1>
                <div className="new-chat-composer-wrap">
                  <ChatInput
                    variant="composer"
                    settingsSlot={projectSettingsSlot}
                    onSend={handleSend}
                    onStop={handleStopStreaming}
                    isStreaming={currentBranchHasStreamingChat}
                    disabled={currentBranchHasStreamingChat}
                    conversationId={null}
                    editValue={editValue}
                    isEditing={Boolean(editTargetNodeId)}
                    onEditValueConsumed={() => setEditValue(null)}
                    onCancelEdit={handleCancelEdit}
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
                    toolPermissionDraft={toolPermissionDraft}
                    getToolPermissionDraft={getToolPermissionDraft}
                    onToolPermissionDraftChange={updateToolPermissionDraft}
                    pendingMultiAgentMode={newConversationMultiAgentMode}
                    onPendingMultiAgentModeChange={setNewConversationMultiAgentMode}
                  />
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                ref={historyRef}
                className={cn(
                  'chat-history-scroll w-full flex-1 overflow-y-scroll pt-4 pb-[140px] flex flex-col items-center custom-scrollbar',
                  isScrolling && 'scrollbar-visible'
                )}
                onScroll={handleScroll}
              >
                <div
                  className="w-[800px] max-w-full flex flex-col px-4"
                >
                  <TranscriptList
                    items={displayTranscriptItems}
                    isLoading={transcriptLoading}
                    transcriptError={transcriptError}
                    onApprovePlan={handleApprovePlan}
                    onRejectPlan={handleRejectPlan}
                    onAnswerPlanQuestion={handleAnswerPlanQuestion}
                    onCopyItem={handleCopyTranscriptItem}
                    onEditUserMessage={handleEditUserMessage}
                    onDeleteUserMessage={handleDeleteUserMessage}
                    planActionPending={planActionPending}
                    planError={planError}
                    renderItem={renderTranscriptItem}
                  />
                  <div ref={messagesEndRef} />
                </div>
              </div>
              <div
                aria-hidden="true"
                className="pointer-events-none absolute bottom-0 left-0 right-[12px] z-[9] h-[150px]"
                style={{
                  background: 'linear-gradient(180deg, color-mix(in srgb, var(--bg-surface) 0%, transparent), var(--bg-surface) 72%)',
                }}
              />
              <footer className="absolute bottom-4 left-1/2 -translate-x-1/2 w-[800px] max-w-[calc(100%-48px)] z-10">
                <ChatInput
                  onSend={handleSend}
                  onStop={handleStopStreaming}
                  isStreaming={currentBranchHasStreamingChat}
                  disabled={currentBranchHasStreamingChat}
                  conversationId={currentConversation?.id || null}
                  editValue={editValue}
                  isEditing={Boolean(editTargetNodeId)}
                  onEditValueConsumed={() => setEditValue(null)}
                  onCancelEdit={handleCancelEdit}
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
                  toolPermissionDraft={toolPermissionDraft}
                  getToolPermissionDraft={getToolPermissionDraft}
                  onToolPermissionDraftChange={updateToolPermissionDraft}
                  pendingMultiAgentMode={newConversationMultiAgentMode}
                  onPendingMultiAgentModeChange={setNewConversationMultiAgentMode}
                  pendingToolApprovals={pendingToolApprovalPrompts}
                  onToolApprovalDecision={handleToolApprovalDecision}
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
          className={cn(
            'app-right-panel flex flex-col shrink-0 transition-[width] duration-200 overflow-hidden',
            resizingSidebar === 'right' && 'is-resizing',
          )}
          style={{
            width: `${outlineCollapsed ? 56 : activeRightPanelWidth}px`,
            background: 'var(--bg-surface)',
            borderLeft: '0.5px solid var(--border)',
          }}
        >
          {!outlineCollapsed && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="调整右侧栏宽度"
              aria-valuemin={activeRightPanelConfig.minWidth}
              aria-valuemax={activeRightPanelConfig.maxWidth}
              aria-valuenow={activeRightPanelWidth}
              tabIndex={0}
              className="sidebar-resize-handle sidebar-resize-handle-right"
              onPointerDown={(event) => beginSidebarResize(event, 'right')}
              onKeyDown={(event) => adjustSidebarWidthFromKeyboard(event, 'right')}
            />
          )}
          <div className="flex justify-between items-center p-3 sticky top-0 z-[1] min-h-[56px]"
               style={{ background: 'var(--bg-surface)' }}>
            {!outlineCollapsed && (
              <div className="flex min-w-0 items-center gap-1">
                <Button
                  variant={rightPanelView === 'outline' ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs"
                  onClick={() => setRightPanelView('outline')}
                >
                  <FileText className="h-3.5 w-3.5" />
                  大纲
                </Button>
                <Button
                  variant={rightPanelView === 'side' ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-8 min-w-0 gap-1.5 px-2 text-xs"
                  onClick={() => setRightPanelView('side')}
                  aria-label="运行"
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                  <span className="min-w-0 truncate">运行</span>
                  {sideRunTopLevelCount > 0 && (
                    <span
                      className="ml-0.5 rounded-full px-1.5 py-0.5 text-[10px]"
                      style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-secondary)' }}
                    >
                      {sideRunTopLevelCount}
                    </span>
                  )}
                </Button>
              </div>
            )}
            <TextTooltip content={outlineCollapsed ? '展开大纲' : '收起大纲'}>
              <Button
                variant="ghost"
                size="sm"
                className="app-panel-toggle"
                onClick={() => setOutlineCollapsed(!outlineCollapsed)}
                aria-label={outlineCollapsed ? '展开大纲' : '收起大纲'}
              >
                {outlineCollapsed ? <PanelRightOpen className="h-5 w-5" /> : <PanelRightClose className="h-5 w-5" />}
              </Button>
            </TextTooltip>
          </div>

          {!outlineCollapsed && (
            <>
              {rightPanelView === 'outline' ? (
                <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden custom-scrollbar">
                  {outline.map((item, idx) => (
                    <TextTooltip key={idx} content={item.text} side="left">
                      <div
                        className="flex items-center py-2 px-3 cursor-pointer rounded-lg mx-2 my-0.5 transition-colors"
                        style={{ color: 'var(--fg-85)' }}
                        onClick={() => handleJumpToMessage(item.originalIndex)}
                        onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                        onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
                      >
                        <span className="truncate text-sm">{item.text}</span>
                      </div>
                    </TextTooltip>
                  ))}
                </div>
              ) : selectedSideRunItem ? (
                <div className="flex min-h-0 flex-1 flex-col">
                  <div className="flex shrink-0 items-center gap-2 px-3 pb-3">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2"
                      onClick={() => setSelectedSideRunId(
                        selectedSideRunItem.run.parentRunId || null,
                      )}
                    >
                      <ArrowLeft className="h-3.5 w-3.5" />
                    </Button>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold" style={{ color: 'var(--fg-secondary)' }}>
                        {getSideRunTitle(selectedSideRunItem.run)}
                      </div>
                      <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {getSlashRunLabel(selectedSideRunItem.run.kind, selectedSideRunItem.run.pendingUserMessage)} · {selectedSideRunItem.run.runId.slice(0, 12)}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      {renderSideRunActions(selectedSideRunItem.draft)}
                    </div>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar">
                    {selectedSideRunItem.run.kind === 'workflow' && renderWorkflowOverview(selectedSideRunItem)}
                    {selectedSideRunItem.run.kind !== 'workflow' && renderSideRunBody(selectedSideRunItem.draft)}
                  </div>
                </div>
              ) : (
                <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar">
                  {sideRunTopLevelCount === 0 && (
                    <div className="rounded-lg border px-3 py-4 text-sm" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
                      暂无运行任务。
                    </div>
                  )}
                  {sideRunGroups.map((group) => (
                    <section key={group.kind} className="flex flex-col gap-2">
                      <div className="px-1 text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>
                        {getSideRunGroupLabel(group.kind)}
                      </div>
                      {group.runs.map((item) => (
                        <div
                          key={item.run.runId}
                          role="button"
                          tabIndex={0}
                          className="rounded-lg border p-0 text-left transition-colors"
                          style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated-secondary, rgba(255,255,255,0.03))' }}
                          onClick={() => setSelectedSideRunId(item.run.runId)}
                          onKeyDown={(event) => {
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            event.preventDefault();
                            setSelectedSideRunId(item.run.runId);
                          }}
                        >
                          <div className="flex items-center gap-2 px-3 py-2">
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-sm font-semibold" style={{ color: 'var(--fg-secondary)' }}>
                                {getSideRunTitle(item.run)}
                              </div>
                              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                                <span className="truncate">{getSlashRunLabel(item.run.kind, item.run.pendingUserMessage)}</span>
                                <span>·</span>
                                <span>{item.run.runId.slice(0, 12)}</span>
                              </div>
                            </div>
                            <span className="shrink-0 text-xs" style={{ color: getSideRunStatusColor(item.run) }}>
                              {getSideRunStatusText(item.run)}
                            </span>
                            <div className="flex shrink-0 items-center gap-1">
                              {renderSideRunActions(item.draft)}
                            </div>
                          </div>
                          {item.run.kind === 'workflow' && item.steps.length > 0 && (
                            <div className="border-t px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
                              {item.steps.length} 个子步骤
                            </div>
                          )}
                        </div>
                      ))}
                    </section>
                  ))}
                </div>
              )}
            </>
          )}
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

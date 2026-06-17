import { useEffect, useState, useRef, useLayoutEffect, useCallback } from 'react';
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
  Plus, X, MoreHorizontal, ChevronLeft, ChevronRight,
  Copy, Check, Pencil, Loader2, RotateCcw, Network, MessageSquare, Trash2, FileText, Download, Settings,
  ChevronDown, Brain,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { rehypeMermaid } from 'react-markdown-mermaid';
import { conversationApi } from '../api/conversation';
import { useConversationStore } from '../store/conversationStore';
import { useModelStore } from '../store/modelStore';
import { useNavigationStore } from '../store/navigationStore';
import { useStreamingManager } from '../hooks/useStreamingManager';
import { streamManager } from '../services/streamManager';
import { ChatInput } from '../components/ChatInput';
import TreeView from './TreeView';

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

/* ---------- Collapsible thinking (reasoning) block ---------- */

function ThinkingBlock({ reasoning, streaming }: { reasoning: string; streaming?: boolean }) {
  // 默认折叠；流式进行中也保持折叠（用户可手动展开看实时思考）。
  const [expanded, setExpanded] = useState(false);
  if (!reasoning) return null;
  return (
    <div className="w-fit max-w-full mb-1.5 rounded-lg overflow-hidden" style={{ border: '0.5px solid var(--border)', background: 'var(--bg-button-tertiary-hover)' }}>
      <button
        className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs cursor-pointer bg-transparent border-none w-full"
        style={{ color: 'var(--fg-tertiary)' }}
        onClick={() => setExpanded((v) => !v)}
      >
        <Brain className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
        <span>{streaming ? '思考中…' : '思考过程'}</span>
        {streaming && <Loader2 className="h-3 w-3 animate-spin" style={{ color: 'var(--icon-accent)' }} />}
        <ChevronDown
          className="h-3.5 w-3.5 ml-auto transition-transform"
          style={{ transform: expanded ? 'rotate(180deg)' : 'none' }}
        />
      </button>
      {expanded && (
        <div
          className="px-3 py-2 text-xs whitespace-pre-wrap break-words"
          style={{ borderTop: '0.5px solid var(--border)', color: 'var(--fg-tertiary)', maxHeight: '320px', overflowY: 'auto', fontFamily: 'var(--font-mono)', lineHeight: 1.6 }}
        >
          {reasoning}
        </div>
      )}
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
  const [previewFile, setPreviewFile] = useState<{ name: string; content: string } | null>(null);
  const scrollTimeoutRef = useRef<number | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const pendingScrollId = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const userScrollingRef = useRef(false);
  const scrollEndTimeoutRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);

  const { chatViewMode, toggleChatViewMode, openSettings } = useNavigationStore();

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
    clearCurrentConversation, updateConversationTitle, refreshMessages,
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
    streamDuration, streamStatus, pendingUserMessage, currentNodeId: streamNodeId,
  } = useStreamingManager(currentConversation?.id ?? null);

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
    });
    return unsubscribe;
  }, [refreshMessages, loadConversations]);

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
    loadConversations();
  }, []);

  const handleSelectConversation = async (id: string) => {
    if (currentConversation && historyRef.current) {
      setScrollPositions(prev => ({
        ...prev,
        [currentConversation.id]: historyRef.current!.scrollTop
      }));
    }
    pendingScrollId.current = id;
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
      const mention = m.role === 'user' ? parseFileMention(m.content) : null;
      const displayContent = mention ? mention.cleanContent : m.content;
      const roleLabel = m.role === 'user' ? '**User**' : '**Assistant**';
      lines.push(`### ${roleLabel}`);
      lines.push('');
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
      const newConv = await createConversation({ title: files[0]?.name?.slice(0, 20) || 'New' });
      if (!newConv) return;
      convId = newConv.id;
    }
    for (const file of files) {
      try {
        const res = await conversationApi.uploadImport(convId, file);
        setAttachedFiles(prev => prev.includes(res.filename) ? prev : [...prev, res.filename]);
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

  const handleSend = async (val: string, modelId?: string, _systemPrompt?: string) => {
    if (!val.trim()) return;
    // 仅阻止向“当前对话”重复发送（它已在流式中）。其他对话的流不受影响，
    // 因此可以在后台对话流式的同时，向另一对话发送——真正的多并发。
    if (isStreaming) return;
    setShouldAutoScroll(true);

    let conversationId = currentConversation?.id;
    if (!conversationId) {
      const newConv = await createConversation({ title: val.slice(0, 20) });
      if (!newConv) {
        console.error('Failed to create conversation');
        return;
      }
      conversationId = newConv.id;
    }

    let finalContent = val;
    if (attachedFiles.length > 0) {
      const parts: string[] = [];
      for (const fname of attachedFiles) {
        try {
          const resp = await fetch(`/api/conversations/${conversationId}/imports/${encodeURIComponent(fname)}`);
          if (resp.ok) {
            const text = await resp.text();
            parts.push(`=== FILE: ${fname} ===\n${text}\n=== END FILE: ${fname} ===`);
          }
        } catch (_) {}
      }
      if (parts.length > 0) {
        finalContent = "'''USER MENTIONED FILES: " + attachedFiles.join(' ') + " '''\n\n" + parts.join('\n\n') + "\n\n---\n\n" + val;
      }
      setAttachedFiles([]);
    }
    // 第三个参数是乐观渲染的用户气泡文本（显示用户输入的原文）。
    // 推理设置从 modelStore 的当前值读取（已确认值），随请求透传。
    const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
    await startStreaming(
      conversationId,
      {
        content: finalContent,
        model_id: modelId,
        reasoning_effort: currentReasoningEffort,
        thinking_enabled: currentThinkingEnabled,
      },
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
      await conversationApi.deleteNode(currentConversation.id, nodeId);
      await selectConversation(currentConversation.id);
    } catch (err) {
      console.error('删除失败:', err);
    }
  };

  const handleRetry = async (assistantNodeId: string, userContent: string) => {
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

  const outline = messages
    .map((m, index) => ({ ...m, originalIndex: index }))
    .filter((m) => m.role === 'user')
    .map((m) => {
      const mention = parseFileMention(m.content);
      const clean = mention ? mention.cleanContent : m.content;
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
    switch (streamStatus) {
      case 'error': return '生成出错';
      case 'stopped': return '已停止';
      default: return null;
    }
  };

  // Parse '''USER MENTIONED FILES: ...''' prefix from message content

  const renderMsg = (m: typeof messages[0], index: number) => {
    const prevUserMessage = index > 0 && messages[index - 1]?.role === 'user'
      ? messages[index - 1]
      : null;
    const fileMention = m.role === 'user' ? parseFileMention(m.content) : null;
    const displayContent = fileMention ? fileMention.cleanContent : m.content;

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
          {fileMention && (
            <div className="max-w-full w-fit mb-1 px-2.5 py-1.5 rounded-lg text-xs flex flex-wrap items-center gap-1.5"
                 style={{ background: 'var(--accent-soft)', border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
              <FileText className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
              {fileMention.fileNames.map((fn, fi) => (
                <span key={fi} className="px-1.5 py-0.5 rounded text-[11px] font-medium cursor-pointer transition-colors"
                      style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-active)'; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--accent-soft)'; }}
                      onClick={() => handlePreviewFile(fn)}>{fn}</span>
              ))}
            </div>
          )}
          {m.role === 'assistant' && m.reasoning && (
            <ThinkingBlock reasoning={m.reasoning} />
          )}
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
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeMermaid as any]}
              components={markdownComponents}
            >
              {displayContent}
            </ReactMarkdown>
          </div>
          {m.role === 'assistant' && m.generation_info && (
            <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
              <span>{formatDuration(m.generation_info.duration_ms)}</span>
              {m.generation_info.status !== 'completed' && (
                <span className={cn(
                  m.generation_info.status === 'error' ? 'text-destructive' : 'text-amber-500'
                )}>
                  {m.generation_info.status === 'stopped' ? '已停止' : '生成出错'}
                </span>
              )}
            </div>
          )}
          <div className="flex items-center gap-1 mt-1">
            <Button
              variant="ghost"
              size="sm"
              className="opacity-0 group-hover:opacity-100 transition-opacity h-7 w-7 p-0"
              onClick={() => handleCopy(m.content, m.id)}
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
                onClick={() => handleRetry(m.node_id, prevUserMessage.content)}
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
        className="flex flex-col transition-[width] duration-200 overflow-x-hidden"
        style={{
          width: sidebarCollapsed ? '56px' : '260px',
          background: 'var(--bg-surface)',
          borderRight: '0.5px solid var(--border)',
        }}
      >
        {/* Header */}
        <div className="flex justify-between items-center p-3 flex-shrink-0 min-h-[56px]"
             style={{ background: 'var(--bg-surface)' }}>
          {!sidebarCollapsed && (
            <Button
              size="sm"
              onClick={() => clearCurrentConversation()}
              className="font-semibold"
              style={{
                background: 'var(--accent-soft)',
                color: 'var(--icon-accent)',
                border: 'none',
              }}
            >
              <Plus className="h-4 w-4 mr-1" />
              新建对话
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          >
            {sidebarCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
          </Button>
        </div>

        {/* Conversation list — scrollable */}
        {!sidebarCollapsed && (
          <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0">
            {[...conversations]
              .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
              .map((c) => (
                <div
                  key={c.id}
                  className={cn(
                    'flex items-center justify-between py-2 px-3 cursor-pointer rounded-lg mx-2 my-0.5 transition-colors',
                  )}
                  onClick={() => handleSelectConversation(c.id)}
                  style={{
                    ...(c.id === currentConversation?.id
                      ? { background: 'var(--bg-button-tertiary-active)' }
                      : {}),
                  }}
                  onMouseEnter={(e) => {
                    if (c.id !== currentConversation?.id) {
                      (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                    }
                    setHoveredId(c.id);
                  }}
                  onMouseLeave={(e) => {
                    if (c.id !== currentConversation?.id) {
                      (e.currentTarget as HTMLElement).style.background = '';
                    }
                    setHoveredId(null);
                  }}
                >
                  <span className="flex-1 mr-2 truncate text-sm">
                    {c.title || '未命名'}
                  </span>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="sm"
                        className={cn(
                          'h-7 w-7 p-0 transition-opacity',
                          hoveredId === c.id ? 'opacity-100' : 'opacity-0'
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
              ))}
          </div>
        )}

        {/* Footer spacer — keeps scroll area from reaching the bottom */}
        <div className="flex-shrink-0 h-2" style={{ borderTop: '0.5px solid var(--border)' }} />
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
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 w-8 p-0"
                  onClick={() => openSettings('providers')}
                >
                  <Settings className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>设置</TooltipContent>
            </Tooltip>
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
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{pendingUserMessage}</ReactMarkdown>
                      </div>
                    </div>
                  </div>
                )}
                {showStreamBlock && (
                  <div className="w-full my-2 flex flex-col items-start">
                    <div className="flex flex-col items-start max-w-full">
                      {streamedReasoning && (
                        <ThinkingBlock reasoning={streamedReasoning} streaming={streamStatus === 'streaming'} />
                      )}
                      <div
                        className="max-w-full w-fit px-3 py-2 rounded-2xl rounded-bl-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
                        style={{
                          color: 'var(--fg-secondary)',
                          fontSize: 'var(--codex-chat-font-size)',
                          lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
                        }}
                      >
                        {streamedContent ? (
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{streamedContent}</ReactMarkdown>
                        ) : (
                          <div className="flex items-center gap-2">
                            <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
                            <span className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>思考中...</span>
                          </div>
                        )}
                      </div>
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
            <footer className="absolute bottom-4 left-1/2 -translate-x-1/2 w-[800px] max-w-[calc(100%-48px)] z-10">
              <ChatInput
                onSend={handleSend}
                onStop={abortStreaming}
                isStreaming={isStreaming}
                disabled={isStreaming}
                conversationId={currentConversation?.id || null}
                editValue={editValue}
                onEditValueConsumed={() => setEditValue(null)}
                attachedFiles={attachedFiles}
                onFilesPicked={handleFilesPicked}
                onRemoveFile={handleRemoveFile}
              />
            </footer>
          </>
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
              className="h-8 w-8 p-0"
              onClick={() => setOutlineCollapsed(!outlineCollapsed)}
            >
              {outlineCollapsed ? <ChevronLeft className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
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

      {/* File preview dialog */}
      <Dialog open={!!previewFile} onOpenChange={(open) => { if (!open) setPreviewFile(null); }}>
        <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {previewFile?.name}
            </DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-auto mt-2 rounded-md bg-muted/50 border p-4">
            <pre className="text-sm whitespace-pre-wrap break-words font-mono leading-relaxed">
              {previewFile?.content}
            </pre>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}










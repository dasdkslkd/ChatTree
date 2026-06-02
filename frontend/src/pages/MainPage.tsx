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
  Copy, Check, Pencil, Loader2, RotateCcw, Network, MessageSquare, Trash2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { rehypeMermaid } from 'react-markdown-mermaid';
import { conversationApi } from '../api/conversation';
import { useConversationStore } from '../store/conversationStore';
import { useNavigationStore } from '../store/navigationStore';
import { useStreaming } from '../hooks/useStreaming';
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

/* ---------- Component ---------- */
export default function ChatPage() {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
  const [scrollPositions, setScrollPositions] = useState<Record<string, number>>({});
  const [isScrolling, setIsScrolling] = useState(false);
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  const [pendingUserMessageConvId, setPendingUserMessageConvId] = useState<string | null>(null);
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  const [editValue, setEditValue] = useState<string | null>(null);
  const scrollTimeoutRef = useRef<number | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const pendingScrollId = useRef<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const userScrollingRef = useRef(false);
  const scrollEndTimeoutRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);

  const { chatViewMode, toggleChatViewMode } = useNavigationStore();

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
    clearCurrentConversation, updateConversationTitle,
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

  const { streamedContent, startStreaming, reset, isStreaming, abortStreaming, streamingConversationId, streamDuration, streamStatus } = useStreaming({
    onComplete: async (_fullContent, completedConversationId) => {
      reset();
      if (pendingUserMessageConvId === completedConversationId) {
        setPendingUserMessage(null);
        setPendingUserMessageConvId(null);
      }
      await selectConversation(completedConversationId);
    },
    onError: async (_error, errorConversationId) => {
      reset();
      if (pendingUserMessageConvId === errorConversationId) {
        setPendingUserMessage(null);
        setPendingUserMessageConvId(null);
      }
      await selectConversation(errorConversationId);
    },
  });

  const shouldAutoScrollRef = useRef(shouldAutoScroll);
  shouldAutoScrollRef.current = shouldAutoScroll;

  useEffect(() => {
    if (isStreaming && streamingConversationId === currentConversation?.id && shouldAutoScrollRef.current && !userScrollingRef.current) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [streamedContent, isStreaming, streamingConversationId, currentConversation?.id, scrollToBottom]);

  useEffect(() => {
    if (pendingUserMessage && pendingUserMessageConvId === currentConversation?.id) {
      requestAnimationFrame(() => scrollToBottom(false));
    }
  }, [pendingUserMessage, pendingUserMessageConvId, currentConversation?.id, scrollToBottom]);

  useEffect(() => {
    (async () => {
      await reset();
      await loadConversations();
    })();
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

  const handleSend = async (val: string, modelId?: string, _systemPrompt?: string) => {
    if (!val.trim()) return;
    setPendingUserMessage(val);
    setPendingUserMessageConvId(currentConversation?.id || null);
    setShouldAutoScroll(true);

    let conversationId = currentConversation?.id;
    if (!conversationId) {
      const newConv = await createConversation({ title: val.slice(0, 20) });
      if (!newConv) {
        console.error('Failed to create conversation');
        setPendingUserMessage(null);
        setPendingUserMessageConvId(null);
        return;
      }
      conversationId = newConv.id;
      setPendingUserMessageConvId(conversationId);
    }

    await startStreaming(conversationId, { content: val, model_id: modelId });
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
    try {
      await conversationApi.deleteNode(currentConversation.id, assistantNodeId);
      await selectConversation(currentConversation.id);
      setPendingUserMessage(userContent);
      setPendingUserMessageConvId(currentConversation.id);
      setShouldAutoScroll(true);
      await startStreaming(currentConversation.id, { content: userContent });
    } catch (err) {
      console.error('重试失败:', err);
      setPendingUserMessage(null);
      setPendingUserMessageConvId(null);
      if (currentConversation) {
        await selectConversation(currentConversation.id);
      }
    }
  };

  const handleEditUserMessage = async (nodeId: string, parentNodeId: string | undefined, userContent: string) => {
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

  const outline = messages
    .map((m, index) => ({ ...m, originalIndex: index }))
    .filter((m) => m.role === 'user')
    .map((m) => ({
      text: m.content.slice(0, 20) + (m.content.length > 20 ? '...' : ''),
      originalIndex: m.originalIndex,
    }));

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

  const renderMsg = (m: typeof messages[0], index: number) => {
    const prevUserMessage = index > 0 && messages[index - 1]?.role === 'user'
      ? messages[index - 1]
      : null;

    return (
      <div
        key={m.id}
        id={`message-${index}`}
        className={cn(
          'w-full my-2 flex flex-col group',
          m.role === 'user' ? 'items-end' : 'items-start'
        )}
      >
        <div className="flex flex-col items-start max-w-full">
          <div
            className={cn(
              'max-w-full w-fit px-3 py-2 rounded-[10px] leading-relaxed prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2',
              m.role === 'user'
                ? 'bg-primary text-primary-foreground prose-invert rounded-br-[6px]'
                : 'bg-muted border rounded-bl-[6px]'
            )}
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeMermaid as any]}
              components={markdownComponents}
            >
              {m.content}
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
                onClick={() => handleEditUserMessage(m.node_id, m.parent_node_id, m.content)}
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
    <div className="flex h-screen bg-background">
      {/* Left conversation list (collapsible) */}
      <nav
        className="flex flex-col transition-[width] duration-200 overflow-y-auto overflow-x-hidden custom-scrollbar border-r bg-background"
        style={{ width: sidebarCollapsed ? '56px' : '260px' }}
      >
        <div className="flex justify-between items-center p-3 sticky top-0 bg-background z-[1] min-h-[56px]">
          {!sidebarCollapsed && (
            <Button size="sm" onClick={() => clearCurrentConversation()}>
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

        {!sidebarCollapsed && conversations.map((c) => (
          <Tooltip key={c.id}>
            <TooltipTrigger asChild>
              <div
                className={cn(
                  'flex items-center justify-between py-2 px-3 cursor-pointer rounded-md mx-2 my-0.5 transition-colors hover:bg-muted',
                  c.id === currentConversation?.id && 'bg-accent hover:bg-accent'
                )}
                onClick={() => handleSelectConversation(c.id)}
                onMouseEnter={() => setHoveredId(c.id)}
                onMouseLeave={() => setHoveredId(null)}
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
            </TooltipTrigger>
            <TooltipContent side="right">{c.title || '未命名'}</TooltipContent>
          </Tooltip>
        ))}
      </nav>

      {/* Center: title bar + content (chat or tree) */}
      <section className="flex-1 flex flex-col overflow-hidden relative bg-background">
        {/* Title bar with view toggle */}
        <div className="flex justify-center items-center p-3 sticky top-0 bg-background z-[1] min-h-[56px] border-b">
          <span className="font-semibold">{currentConversation?.title || '请选择对话'}</span>
          {/* View toggle button */}
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 ml-3"
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
                {pendingUserMessage && pendingUserMessageConvId === currentConversation?.id && (
                  <div className="w-full my-2 flex flex-col items-end">
                    <div className="flex flex-col items-start max-w-full">
                      <div className="max-w-full w-fit px-3 py-2 rounded-[10px] rounded-br-[6px] leading-relaxed bg-primary text-primary-foreground prose prose-sm prose-invert max-w-none [&_p]:m-0">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{pendingUserMessage}</ReactMarkdown>
                      </div>
                    </div>
                  </div>
                )}
                {isStreaming && streamingConversationId === currentConversation?.id && (
                  <div className="w-full my-2 flex flex-col items-start">
                    <div className="flex flex-col items-start max-w-full">
                      <div className="max-w-full w-fit px-3 py-2 rounded-[10px] rounded-bl-[6px] leading-relaxed bg-muted border prose prose-sm max-w-none [&_p]:m-0">
                        {streamedContent ? (
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{streamedContent}</ReactMarkdown>
                        ) : (
                          <div className="flex items-center gap-2">
                            <Loader2 className="h-4 w-4 animate-spin" />
                            <span className="text-sm text-muted-foreground">思考中...</span>
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
            <footer className="absolute bottom-5 left-1/2 -translate-x-1/2 w-[800px] max-w-[calc(100%-48px)] z-10">
              <ChatInput
                onSend={handleSend}
                onStop={abortStreaming}
                isStreaming={isStreaming}
                disabled={isStreaming && streamingConversationId === currentConversation?.id}
                conversationId={currentConversation?.id || null}
                streamingConversationId={streamingConversationId}
                editValue={editValue}
                onEditValueConsumed={() => setEditValue(null)}
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
          className="flex flex-col transition-[width] duration-200 overflow-y-auto overflow-x-hidden custom-scrollbar border-l bg-background"
          style={{ width: outlineCollapsed ? '56px' : '280px' }}
        >
          <div className="flex justify-between items-center p-3 sticky top-0 bg-background z-[1] min-h-[56px]">
            {!outlineCollapsed && <span className="font-semibold">大纲</span>}
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
              className="flex items-center py-2 px-3 cursor-pointer rounded-md mx-2 my-0.5 transition-colors hover:bg-muted"
              title={item.text}
              onClick={() => handleJumpToMessage(item.originalIndex)}
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
    </div>
  );
}










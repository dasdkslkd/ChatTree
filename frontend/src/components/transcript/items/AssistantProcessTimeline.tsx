import { useState } from 'react';
import { Check, ChevronRight, Loader2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { messageApi, type ToolResultSlice } from '../../../api/message';
import type { TranscriptItem } from '../../../types/transcript';
import type { AssistantProcessRenderProps, AssistantTimelineBlock, ToolRenderItem } from '../../../utils/assistantTimeline';
import { formatProcessedDuration } from '../../../utils/assistantTimelineFolding';
import { formatToolOutput } from '../../../utils/toolDisplay';
import MarkdownContent from '../../MarkdownContent';

export function getStreamStatusLabel(status: AssistantProcessRenderProps['status'], errorMessage: string | null): string | null {
  if (status === 'error') return errorMessage || '生成失败';
  if (status === 'stopping') return '正在停止...';
  if (status === 'stopped') return '已停止';
  return null;
}

function ThoughtBlock({ reasoning, streaming }: { reasoning: string; streaming?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!reasoning) return null;
  const label = streaming ? '思考中' : '思考完成';
  return (
    <div className={cn('thought', expanded && 'expanded')}>
      <button
        type="button"
        className="thought-head"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
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

function ContentBlock({ block }: { block: Extract<AssistantTimelineBlock, { type: 'content' }> }) {
  return (
    <div
      key={block.key}
      className="max-w-full min-w-0 rounded-lg px-3 py-2 text-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
      style={{ color: 'var(--fg-secondary)' }}
    >
      <MarkdownContent enableMermaid>{block.content}</MarkdownContent>
    </div>
  );
}

function MarkerBlock({ block }: { block: Extract<AssistantTimelineBlock, { type: 'marker' }> }) {
  return (
    <div
      key={block.key}
      className="my-1 flex items-center gap-2 px-3 py-1 text-xs"
      style={{ color: 'var(--fg-tertiary)' }}
    >
      <span className="h-px w-6 shrink-0" style={{ background: 'var(--border-subtle)' }} />
      <span className="min-w-0 truncate">{block.content}</span>
    </div>
  );
}

function renderTimelineBlock(
  block: AssistantTimelineBlock,
  _item: TranscriptItem,
  props: AssistantProcessRenderProps,
) {
  if (block.type === 'reasoning') {
    return (
      <ThoughtBlock
        key={block.key}
        reasoning={block.reasoning}
        streaming={block.key === props.activeReasoningKey}
      />
    );
  }
  if (block.type === 'tools') {
    return <ToolCallGroup key={block.key} items={block.items} />;
  }
  if (block.type === 'marker') {
    return <MarkerBlock key={block.key} block={block} />;
  }
  return <ContentBlock key={block.key} block={block} />;
}

export function AssistantProcessTimeline({
  item,
  props,
}: {
  item: TranscriptItem;
  props: AssistantProcessRenderProps;
}) {
  const timeline = Array.isArray(props.timeline) ? props.timeline : [];
  const statusLabel = getStreamStatusLabel(props.status, props.errorMessage);
  const showPendingBubble = Boolean(props.showPendingBubble && props.pendingUserMessage);
  const showStreamBlock = props.showStreamBlock !== false;
  const [processExpanded, setProcessExpanded] = useState(props.streamingFoldState?.processExpanded ?? true);
  const foldedContentBlocks = processExpanded ? [] : props.streamingFoldState?.contentBlocks ?? [];
  return (
    <div className="contents">
      {showPendingBubble && (
        <div className="w-full my-2 flex flex-col items-end" role="listitem">
          <div className="flex flex-col items-start max-w-full">
            <div
              className="max-w-full rounded-lg px-3 py-2 text-sm prose prose-sm prose-invert max-w-none [&_p]:m-0"
              style={{
                background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
                border: '0.5px solid rgba(217,119,87,0.28)',
                color: 'var(--fg-85)',
              }}
            >
              <MarkdownContent enableMermaid>{props.pendingUserMessage || ''}</MarkdownContent>
            </div>
          </div>
        </div>
      )}
      {showStreamBlock && (
        <div className={cn('w-full flex flex-col items-start', props.compactWithNextAnswer ? 'mt-2 mb-0' : 'my-2')} role="listitem">
          <div className="flex flex-col items-start max-w-full w-full min-w-0">
            {props.streamingFoldState?.canFoldProcess ? (
              <>
                <div className={cn('processed-fold', processExpanded && 'expanded')}>
                  <button
                    type="button"
                    className="processed-fold-button"
                    aria-expanded={processExpanded}
                    onClick={() => setProcessExpanded((value) => !value)}
                  >
                    <span>{props.duration > 0 ? `已处理 ${formatProcessedDuration(props.duration) ?? ''}`.trim() : '已处理'}</span>
                    <ChevronRight className="processed-fold-chevron" />
                  </button>
                </div>
                <div className={cn('processed-blocks-shell', processExpanded && 'expanded')} aria-hidden={!processExpanded}>
                  <div className="processed-blocks-inner">
                    {props.streamingFoldState.visibleBlocks.map((block) => renderTimelineBlock(block, item, props))}
                  </div>
                </div>
                {foldedContentBlocks.length > 0 && (
                  <div className="w-full flex flex-col items-start">
                    {foldedContentBlocks.map((block) => renderTimelineBlock(block, item, props))}
                  </div>
                )}
              </>
            ) : (
              <div className="w-full flex flex-col items-start">
                {timeline.map((block) => renderTimelineBlock(block, item, props))}
              </div>
            )}
            {timeline.length === 0 && props.status === 'streaming' && (
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
            {statusLabel && (
              <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
                <span className="text-destructive">{statusLabel}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

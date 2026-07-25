import { useState } from 'react';
import { Check, ChevronRight, Loader2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { TranscriptItem } from '../../../types/transcript';
import { formatProcessedDuration, getStreamingTimelineFoldState } from '../../../utils/assistantTimelineFolding';
import MarkdownContent from '../../MarkdownContent';

export type ToolRenderItem = {
  key: string;
  name: string;
  summary: string;
  argsText: string;
  outputText: string;
  status: 'done' | 'error' | 'running';
};

export type ProcessRenderBlock =
  | { type: 'reasoning'; key: string; reasoning: string; streaming: boolean }
  | { type: 'content'; key: string; content: string; streaming: boolean }
  | { type: 'tools'; key: string; items: ToolRenderItem[] };

export type AssistantProcessRenderProps = {
  timeline: ProcessRenderBlock[];
  status: string | null;
  duration: number;
  errorMessage: string | null;
  showStatusLabel?: boolean;
};

export function getStreamStatusLabel(status: AssistantProcessRenderProps['status'], errorMessage: string | null): string | null {
  if (status === 'error') return errorMessage || '生成失败';
  if (status === 'stopping') return '正在停止...';
  if (status === 'stopped') return '已停止';
  return null;
}

function ThoughtBlock({ reasoning, streaming }: { reasoning: string; streaming?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  if (!reasoning) return null;
  return (
    <div className={cn('thought', expanded && 'expanded')}>
      <button
        type="button"
        className="thought-head"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <ChevronRight className="thought-chevron" />
        <span>{streaming ? '思考中' : '思考完成'}</span>
      </button>
      <div className="thought-body-shell" aria-hidden={!expanded}>
        <div className="thought-body-clip">
          <div className="thought-body custom-scrollbar">{reasoning}</div>
        </div>
      </div>
    </div>
  );
}

function GenericToolCallCard({ item }: { item: ToolRenderItem }) {
  const [expanded, setExpanded] = useState(false);

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
        {item.outputText && <pre className="tc-output custom-scrollbar">{item.outputText}</pre>}
      </div>
    </div>
  );
}

function ToolCallGroup({ items }: { items: ToolRenderItem[] }) {
  const [collapsed, setCollapsed] = useState(false);
  if (items.length === 0) return null;
  if (items.length === 1) return <GenericToolCallCard item={items[0]} />;
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
        {items.map((item) => <GenericToolCallCard key={item.key} item={item} />)}
      </div>
    </div>
  );
}

function ContentBlock({ block }: { block: Extract<ProcessRenderBlock, { type: 'content' }> }) {
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

function renderTimelineBlock(block: ProcessRenderBlock) {
  if (block.type === 'reasoning') {
    return (
      <ThoughtBlock
        key={block.key}
        reasoning={block.reasoning}
        streaming={block.streaming}
      />
    );
  }
  if (block.type === 'tools') return <ToolCallGroup key={block.key} items={block.items} />;
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
  const statusLabel = props.showStatusLabel === false ? null : getStreamStatusLabel(props.status, props.errorMessage);
  const compactWithNextAnswer = Boolean((item as TranscriptItem & { compact_with_next_answer?: boolean }).compact_with_next_answer);
  const foldState = getStreamingTimelineFoldState(
    timeline,
    [],
    { allowProcessOnly: compactWithNextAnswer },
  );
  const [processExpanded, setProcessExpanded] = useState(foldState.processExpanded);
  const visibleBlocks = foldState.canFoldProcess ? foldState.visibleBlocks : timeline;
  const foldedContentBlocks = foldState.canFoldProcess && !processExpanded ? foldState.contentBlocks : [];

  return (
    <div className="contents">
      <div className="w-full flex flex-col items-start my-2" role="listitem">
        <div className="flex flex-col items-start max-w-full w-full min-w-0">
          {foldState.canFoldProcess && (
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
          )}
          {(!foldState.canFoldProcess || processExpanded) && (
            <div className={cn('processed-blocks-shell', processExpanded && 'expanded')} aria-hidden={foldState.canFoldProcess && !processExpanded}>
              <div className="processed-blocks-inner">
                {visibleBlocks.map((block) => renderTimelineBlock(block))}
              </div>
            </div>
          )}
          {foldedContentBlocks.length > 0 && (
            <div className="w-full flex flex-col items-start">
              {foldedContentBlocks.map((block) => renderTimelineBlock(block))}
            </div>
          )}
          {timeline.length === 0 && props.status === 'running' && (
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
    </div>
  );
}

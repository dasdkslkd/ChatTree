import { useState } from 'react';
import { ChevronRight, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { TranscriptItem } from '../../../types/transcript';
import MarkdownContent from '../../MarkdownContent';
import { ToolCallCard } from './ToolCallRenderer';

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
  props,
}: {
  item: TranscriptItem;
  props: AssistantProcessRenderProps;
}) {
  const timeline = Array.isArray(props.timeline) ? props.timeline : [];
  const statusLabel = props.showStatusLabel === false ? null : getStreamStatusLabel(props.status, props.errorMessage);

  return (
    <div className="contents">
      <div className="w-full flex flex-col items-start" role="listitem">
        <div className="flex flex-col items-start max-w-full w-full min-w-0">
          <div className="processed-blocks-shell expanded">
            <div className="processed-blocks-inner">
              {timeline.map((block) => renderTimelineBlock(block))}
            </div>
          </div>
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

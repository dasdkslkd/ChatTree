import { useState } from 'react';
import { ChevronRight, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import { cn } from '@/lib/utils';
import MarkdownContent from '../../MarkdownContent';
import type { TranscriptItem, TranscriptPlanActionHandler } from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';
import { PlanProposalCard, type PlanProposalBlock } from './PlanProposalCard';

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
  raw_content?: unknown;
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
};

type ProcessTimelineBlock =
  | { type: 'reasoning'; key: string; reasoning: string }
  | { type: 'content'; key: string; content: string }
  | { type: 'tools'; key: string; items: ToolRenderItem[] }
  | (PlanProposalBlock & { key: string });

interface AssistantProcessItemProps {
  item: TranscriptItem;
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  planActionPending?: string | null;
  planError?: string | null;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function formatValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function limitToolDisplayText(text: string): string {
  return text.length <= 100 ? text : `${text.slice(0, 97)}...`;
}

function getToolName(toolCall: ToolCallLike | null, toolMessage?: ToolMessageLike | null): string {
  return toolCall?.function?.name || toolCall?.name || toolMessage?.name || 'tool';
}

function getToolRawArgs(toolCall: ToolCallLike | null): unknown {
  return toolCall?.function?.arguments ?? toolCall?.arguments ?? toolCall?.args ?? toolCall?.input;
}

function getToolOutput(toolMessage?: ToolMessageLike | null): string {
  if (!toolMessage) return '';
  return formatValue(toolMessage.raw_content ?? toolMessage.content ?? toolMessage.output ?? toolMessage.result ?? toolMessage.error);
}

function makeToolSummary(name: string, rawArgs: unknown): string {
  const argsText = formatValue(rawArgs).replace(/\s+/g, ' ').trim();
  return limitToolDisplayText(argsText ? `${name} ${argsText}` : name);
}

function makeToolItem(
  toolCall: ToolCallLike | null,
  toolMessage: ToolMessageLike | null,
  fallbackKey: string,
): ToolRenderItem {
  const name = getToolName(toolCall, toolMessage);
  const rawArgs = getToolRawArgs(toolCall);
  const outputText = limitToolDisplayText(getToolOutput(toolMessage));
  return {
    key: toolCall?.id || toolMessage?.tool_call_id || fallbackKey,
    name,
    summary: toolCall ? makeToolSummary(name, rawArgs) : outputText || '工具结果',
    argsText: formatValue(rawArgs),
    outputText,
    status: toolMessage ? (toolMessage.error ? 'error' : 'done') : 'running',
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

function getStringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === 'string' ? value : '';
}

function normalizeTimelineToolCall(record: Record<string, unknown>): ToolCallLike | null {
  const candidate = asRecord(record.tool_call) || asRecord(record.call) || record;
  return candidate ? candidate as ToolCallLike : null;
}

function normalizeTimelineToolMessage(record: Record<string, unknown>): ToolMessageLike | null {
  const candidate = asRecord(record.tool_result) || asRecord(record.result) || record;
  return candidate ? candidate as ToolMessageLike : null;
}

function normalizeBackendTimelineBlock(rawBlock: unknown, index: number): ProcessTimelineBlock | null {
  const record = asRecord(rawBlock);
  if (!record) return null;
  const type = getStringField(record, 'type');
  const key = getStringField(record, 'key') || `${type || 'timeline'}-${index}`;

  if (type === 'plan_proposal') {
    const status = getStringField(record, 'status') || 'awaiting_approval';
    const plan = getStringField(record, 'plan');
    if (!plan.trim()) return null;
    return {
      type: 'plan_proposal',
      key,
      tool_name: getStringField(record, 'tool_name') || 'exit_plan_mode',
      tool_call_id: getStringField(record, 'tool_call_id'),
      plan_id: getStringField(record, 'plan_id'),
      proposal_id: getStringField(record, 'proposal_id'),
      revision: typeof record.revision === 'number' ? record.revision : 1,
      status: status === 'approved' || status === 'rejected' || status === 'superseded'
        ? status
        : 'awaiting_approval',
      plan,
      feedback: typeof record.feedback === 'string' ? record.feedback : null,
    };
  }

  if (type === 'reasoning') {
    const reasoning = getStringField(record, 'reasoning') || getStringField(record, 'content');
    return reasoning.trim() ? { type: 'reasoning', key, reasoning } : null;
  }

  if (type === 'content' || type === 'text') {
    const content = getStringField(record, 'content') || getStringField(record, 'text');
    return content.trim() ? { type: 'content', key, content } : null;
  }

  if (type === 'tools' && Array.isArray(record.items)) {
    const items = record.items as ToolRenderItem[];
    return items.length > 0 ? { type: 'tools', key, items } : null;
  }

  if (type === 'tool_call') {
    const item = makeToolItem(normalizeTimelineToolCall(record), null, key);
    return { type: 'tools', key, items: [item] };
  }

  if (type === 'tool_result') {
    const item = makeToolItem(null, normalizeTimelineToolMessage(record), key);
    return { type: 'tools', key, items: [item] };
  }

  return null;
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

function getProcessTimeline(item: TranscriptItem): ProcessTimelineBlock[] {
  const timeline = Array.isArray(item.props?.timeline) ? item.props.timeline : [];
  if (timeline.length > 0) {
    return timeline
      .map((block, index) => normalizeBackendTimelineBlock(block, index))
      .filter((block): block is ProcessTimelineBlock => Boolean(block));
  }

  const blocks: ProcessTimelineBlock[] = [];
  const interactions = Array.isArray(item.props?.tool_interactions) ? item.props.tool_interactions : [];
  const interactionReasoning: string[] = [];

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
      blocks.push({ type: 'content', key: `content-${interactionIndex}`, content });
    }
    if (toolItems.length > 0) {
      blocks.push({ type: 'tools', key: `tools-${interactionIndex}`, items: toolItems });
    }
  });

  const finalReasoning = stripChronologicalPrefix(item.props?.reasoning, interactionReasoning);
  if (finalReasoning.trim()) {
    blocks.push({ type: 'reasoning', key: 'reasoning-final', reasoning: finalReasoning });
  }

  return blocks;
}

function planProposalItem(item: TranscriptItem, block: PlanProposalBlock): TranscriptItem {
  return {
    ...item,
    id: `${item.id}:${block.proposal_id || block.tool_call_id || block.plan_id || 'plan-proposal'}`,
    plan_id: block.plan_id || item.plan_id || null,
    status: block.status,
    preview: block.plan,
    props: {
      ...(item.props || {}),
      plan: block.plan,
      proposal_id: block.proposal_id,
      tool_call_id: block.tool_call_id,
      revision: block.revision,
      feedback: block.feedback,
    },
  };
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
  return (
    <div className={cn('tool-call', expanded && 'expanded')}>
      <button
        type="button"
        className="tc-header"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <ChevronRight className="tc-chevron" />
        <span className="tc-name">{item.name}</span>
        <span className="tc-summary">{item.summary}</span>
        <span className="tc-status">
          {item.status === 'running' && <Loader2 className="h-3 w-3 animate-spin" />}
          {item.status === 'done' && <CheckCircle2 className="h-3 w-3" />}
          {item.status === 'error' && <XCircle className="h-3 w-3" />}
        </span>
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

function renderProcessTimelineBlock(
  block: ProcessTimelineBlock,
  item: TranscriptItem,
  handlers: Pick<AssistantProcessItemProps, 'onApprovePlan' | 'onRejectPlan' | 'planActionPending' | 'planError'>,
) {
  if (block.type === 'plan_proposal') {
    return (
      <PlanProposalCard
        key={block.key}
        block={block}
        onApprove={() => handlers.onApprovePlan?.(planProposalItem(item, block))}
        onReject={() => handlers.onRejectPlan?.(planProposalItem(item, block))}
        pending={handlers.planActionPending !== null && handlers.planActionPending !== undefined}
        error={handlers.planError}
      />
    );
  }
  if (block.type === 'reasoning') {
    return <ThoughtBlock key={block.key} reasoning={block.reasoning} />;
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
      <MarkdownContent enableMermaid>{block.content}</MarkdownContent>
    </div>
  );
}

export function AssistantProcessItem({
  item,
  onApprovePlan,
  onRejectPlan,
  planActionPending = null,
  planError = null,
}: AssistantProcessItemProps) {
  const timeline = getProcessTimeline(item);
  if (timeline.length > 0) {
    return (
      <div className="w-full flex flex-col items-start" role="listitem">
        {timeline.map((block) => renderProcessTimelineBlock(block, item, {
          onApprovePlan,
          onRejectPlan,
          planActionPending,
          planError,
        }))}
      </div>
    );
  }

  const text = getItemText(item, 'Processing');
  const status = getStatusText(item);
  const streaming = status === 'streaming' || status === 'running';

  return (
    <div className="w-full flex flex-col items-start" role="listitem">
      <ThoughtBlock reasoning={text} streaming={streaming} />
    </div>
  );
}

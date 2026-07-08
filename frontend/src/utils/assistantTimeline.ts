import type { StreamState } from '../services/streamManager';
import type { TranscriptItem } from '../types/transcript';
import {
  extractToolResultEnvelope,
  formatToolArguments,
  formatToolOutput,
  isToolResultError,
  summarizeToolCall,
  type ToolResultEnvelope,
} from './toolDisplay';
import {
  getAssistantFoldedContentBlocks,
  getStreamingTimelineFoldState,
  type StreamingTimelineFoldState,
} from './assistantTimelineFolding';

export type ToolCallLike = {
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

export type ToolMessageLike = {
  name?: string;
  content?: unknown;
  raw_content?: unknown;
  model_visible_content?: unknown;
  output?: unknown;
  result?: unknown;
  error?: unknown;
  envelope?: unknown;
  tool_result_id?: unknown;
  tool_call_id?: string;
};

export type ToolRenderItem = {
  key: string;
  name: string;
  summary: string;
  argsText: string;
  outputText: string;
  status: 'done' | 'error' | 'running';
  resultEnvelope: ToolResultEnvelope | null;
};

export type AssistantTimelineBlock =
  | { type: 'reasoning'; key: string; reasoning: string }
  | { type: 'marker'; key: string; content: string }
  | { type: 'content'; key: string; content: string }
  | { type: 'tools'; key: string; items: ToolRenderItem[] };

export type AssistantProcessRenderProps = {
  live_process?: boolean;
  pendingUserMessage?: string | null;
  showPendingBubble?: boolean;
  showStreamBlock?: boolean;
  showStatusLabel?: boolean;
  timeline: AssistantTimelineBlock[];
  streamingFoldState: StreamingTimelineFoldState<AssistantTimelineBlock>;
  activeReasoningKey: string | null;
  status: StreamState['status'] | string | null;
  duration: number;
  errorMessage: string | null;
  content: string;
  compactWithNextAnswer?: boolean;
  continuation_of_node_id?: string | null;
  continuation_marker?: string | null;
  continuations?: unknown[];
};

const TOOL_DISPLAY_LIMIT = 100;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function getStringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === 'string' ? value : '';
}

function limitToolDisplayText(text: string): string {
  if (text.length <= TOOL_DISPLAY_LIMIT) return text;
  return `${text.slice(0, TOOL_DISPLAY_LIMIT - 3)}...`;
}

export function getToolName(toolCall: ToolCallLike | null, toolMessage?: ToolMessageLike | null): string {
  return toolCall?.function?.name || toolCall?.name || toolMessage?.name || 'tool';
}

export function getToolRawArgs(toolCall: ToolCallLike | null): unknown {
  return toolCall?.function?.arguments ?? toolCall?.arguments ?? toolCall?.args ?? toolCall?.input;
}

export function getToolOutput(toolMessage?: ToolMessageLike | null): string {
  return formatToolOutput(toolMessage);
}

export function makeToolSummary(name: string, rawArgs: unknown): string {
  return limitToolDisplayText(summarizeToolCall(name, rawArgs));
}

export function makeToolItem(
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
    argsText: toolCall ? formatToolArguments(rawArgs) : '',
    outputText,
    status: toolMessage ? (isToolResultError(toolMessage) ? 'error' : 'done') : 'running',
    resultEnvelope: extractToolResultEnvelope(toolMessage),
  };
}

function findToolMessage(toolMessages: ToolMessageLike[], toolCall: ToolCallLike, index: number): ToolMessageLike | null {
  if (toolCall.id) {
    const matched = toolMessages.find((message) => message.tool_call_id === toolCall.id);
    if (matched) return matched;
  }
  return toolMessages[index] ?? null;
}

function normalizeTimelineToolCall(record: Record<string, unknown>): ToolCallLike | null {
  const candidate = asRecord(record.tool_call) || asRecord(record.call) || record;
  return candidate ? candidate as ToolCallLike : null;
}

function normalizeTimelineToolMessage(record: Record<string, unknown>): ToolMessageLike | null {
  const candidate = asRecord(record.tool_result) || asRecord(record.result) || record;
  return candidate ? candidate as ToolMessageLike : null;
}

function normalizeToolItems(items: unknown[], key: string): ToolRenderItem[] {
  return items
    .map((item, index) => {
      const record = asRecord(item);
      if (!record) return null;
      if (
        typeof record.key === 'string'
        && typeof record.name === 'string'
        && typeof record.summary === 'string'
      ) {
        return {
          key: record.key,
          name: record.name,
          summary: record.summary,
          argsText: typeof record.argsText === 'string' ? record.argsText : '',
          outputText: typeof record.outputText === 'string' ? record.outputText : '',
          status: record.status === 'done' || record.status === 'error' || record.status === 'running'
            ? record.status
            : 'done',
          resultEnvelope: asRecord(record.resultEnvelope) as ToolResultEnvelope | null,
        };
      }
      return makeToolItem(normalizeTimelineToolCall(record), normalizeTimelineToolMessage(record), `${key}-${index}`);
    })
    .filter((item): item is ToolRenderItem => Boolean(item));
}

export function normalizePersistedAssistantTimeline(rawTimeline: unknown): AssistantTimelineBlock[] {
  const timeline = Array.isArray(rawTimeline) ? rawTimeline : [];
  const blocks: AssistantTimelineBlock[] = [];
  let pendingToolKey: string | null = null;
  let pendingToolItems: ToolRenderItem[] = [];

  const flushTools = () => {
    if (pendingToolItems.length === 0) return;
    blocks.push({
      type: 'tools',
      key: pendingToolKey || `tools-${blocks.length}`,
      items: pendingToolItems,
    });
    pendingToolKey = null;
    pendingToolItems = [];
  };

  const appendTools = (key: string, items: ToolRenderItem[]) => {
    if (items.length === 0) return;
    pendingToolKey = pendingToolKey || key;
    pendingToolItems.push(...items);
  };

  timeline.forEach((rawBlock, index) => {
    const record = asRecord(rawBlock);
    if (!record) return;
    const type = getStringField(record, 'type');
    const key = getStringField(record, 'key') || `${type || 'timeline'}-${index}`;

    if (type === 'tools' && Array.isArray(record.items)) {
      appendTools(key, normalizeToolItems(record.items, key));
      return;
    }
    if (type === 'tool_call') {
      appendTools(key, [makeToolItem(normalizeTimelineToolCall(record), normalizeTimelineToolMessage(record), key)]);
      return;
    }
    if (type === 'tool_result') {
      appendTools(key, [makeToolItem(null, normalizeTimelineToolMessage(record), key)]);
      return;
    }

    let block: AssistantTimelineBlock | null = null;
    if (type === 'reasoning') {
      const reasoning = getStringField(record, 'reasoning') || getStringField(record, 'content');
      block = reasoning.trim() ? { type: 'reasoning', key, reasoning } : null;
    } else if (type === 'marker') {
      const content = getStringField(record, 'content') || getStringField(record, 'text') || getStringField(record, 'marker');
      block = content.trim() ? { type: 'marker', key, content } : null;
    } else if (type === 'content' || type === 'text') {
      const content = getStringField(record, 'content') || getStringField(record, 'text');
      block = content.trim() ? { type: 'content', key, content } : null;
    }

    if (block) {
      flushTools();
      blocks.push(block);
    }
  });

  flushTools();
  return blocks;
}

export function appendAssistantContinuations(
  timeline: AssistantTimelineBlock[],
  continuations: unknown,
): AssistantTimelineBlock[] {
  if (!Array.isArray(continuations) || continuations.length === 0) return timeline;
  const blocks = [...timeline];
  continuations.forEach((rawContinuation, index) => {
    const continuation = asRecord(rawContinuation);
    if (!continuation) return;
    const marker = getStringField(continuation, 'marker') || getStringField(continuation, 'continuation_marker');
    const continuationBlocks = normalizePersistedAssistantTimeline(continuation.timeline);
    if (marker.trim()) {
      blocks.push({ type: 'marker', key: `continuation-marker-${index}`, content: marker });
    }
    blocks.push(...continuationBlocks.map((block) => ({
      ...block,
      key: `continuation-${index}-${block.key}`,
    })));
  });
  return blocks;
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

export function normalizeLegacyToolInteractions(input: {
  content?: unknown;
  reasoning?: unknown;
  tool_interactions?: unknown[];
  tool_calls?: unknown[];
  tool_results?: unknown[];
}): AssistantTimelineBlock[] {
  const blocks: AssistantTimelineBlock[] = [];
  const interactions = Array.isArray(input.tool_interactions) ? input.tool_interactions : [];

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

    const finalReasoning = stripChronologicalPrefix(input.reasoning, interactionReasoning);
    const finalContent = stripChronologicalPrefix(input.content, interactionContent);
    if (finalReasoning.trim()) blocks.push({ type: 'reasoning', key: 'reasoning-final', reasoning: finalReasoning });
    if (finalContent.trim()) blocks.push({ type: 'content', key: 'content-final', content: finalContent });
    return blocks;
  }

  const reasoning = typeof input.reasoning === 'string' ? input.reasoning : '';
  const content = typeof input.content === 'string' ? input.content : '';
  const toolItems = getAssistantToolItems(input);
  if (reasoning) blocks.push({ type: 'reasoning', key: 'reasoning', reasoning });
  if (toolItems.length > 0) blocks.push({ type: 'tools', key: 'tools', items: toolItems });
  if (content.trim()) blocks.push({ type: 'content', key: 'content', content });
  return blocks;
}

export function normalizeLiveAssistantTimeline(run: StreamState): AssistantTimelineBlock[] {
  return normalizeLegacyToolInteractions({
    content: run.content,
    reasoning: run.reasoning,
    tool_interactions: run.toolInteractions,
  });
}

function withoutTimelineKeys(
  timeline: AssistantTimelineBlock[],
  keys: Iterable<string>,
): AssistantTimelineBlock[] {
  const keySet = new Set(keys);
  if (keySet.size === 0) return timeline;
  return timeline.filter((block) => !keySet.has(block.key));
}

export function getActiveReasoningKey(
  timeline: AssistantTimelineBlock[],
  run: Pick<StreamState, 'status' | 'reasoningActive'>,
): string | null {
  if (run.status !== 'streaming') return null;
  for (let i = timeline.length - 1; i >= 0; i -= 1) {
    if (timeline[i].type === 'reasoning') {
      const hasLaterBlock = timeline.slice(i + 1).some((block) => block.type !== 'reasoning');
      return run.reasoningActive || !hasLaterBlock ? timeline[i].key : null;
    }
  }
  return null;
}

export function createProcessRenderPropsFromRun(
  run: StreamState,
  options?: {
    splitAnswer?: boolean;
  },
): AssistantProcessRenderProps {
  const fullTimeline = normalizeLiveAssistantTimeline(run);
  const streamingFoldedContentBlocks = getAssistantFoldedContentBlocks({
    content: run.content,
    reasoning: run.reasoning,
    tool_interactions: run.toolInteractions,
  });
  const finalContentKeys = streamingFoldedContentBlocks.map((block) => block.key);
  const timeline = options?.splitAnswer
    ? withoutTimelineKeys(fullTimeline, finalContentKeys)
    : fullTimeline;
  const continuationOfNodeId = typeof run.metadata?.continuation_of_node_id === 'string'
    ? run.metadata.continuation_of_node_id
    : run.metadata?.origin === 'plan_approval' || run.metadata?.origin === 'plan_question_answer' || run.metadata?.origin === 'plan_reject' || run.metadata?.origin === 'plan_rejection'
      ? run.anchorNodeId
      : null;
  const continuationMarker = typeof run.metadata?.continuation_marker === 'string'
    ? run.metadata.continuation_marker
    : run.metadata?.origin === 'plan_approval'
      ? '计划已批准，开始实现'
      : run.metadata?.origin === 'plan_question_answer' || run.metadata?.origin === 'plan_reject' || run.metadata?.origin === 'plan_rejection'
        ? '计划反馈已提交，继续计划'
        : null;
  return {
    live_process: true,
    continuation_of_node_id: continuationOfNodeId,
    continuation_marker: continuationMarker,
    pendingUserMessage: run.pendingUserMessage,
    showPendingBubble: Boolean(run.pendingUserMessage),
    showStreamBlock: run.status !== 'idle',
    showStatusLabel: options?.splitAnswer ? false : true,
    timeline,
    streamingFoldState: getStreamingTimelineFoldState(
      timeline,
      options?.splitAnswer ? [] : finalContentKeys,
      { allowProcessOnly: true },
    ),
    activeReasoningKey: getActiveReasoningKey(timeline, run),
    status: run.status,
    duration: run.duration,
    errorMessage: run.errorMessage,
    content: run.content,
  };
}

export function createLiveAssistantProcessItem(
  run: StreamState,
  options?: {
    splitAnswer?: boolean;
  },
): TranscriptItem {
  const nodeId = run.targetNodeId || run.nodeId || null;
  return {
    id: `live-${run.runId}-process`,
    type: 'assistant_process',
    conversation_id: run.conversationId,
    node_id: nodeId,
    anchor_node_id: run.anchorNodeId,
    run_id: run.runId,
    status: run.status,
    visibility: 'main',
    preview: run.content || run.reasoning || run.pendingUserMessage || '',
    props: createProcessRenderPropsFromRun(run, options) as unknown as Record<string, unknown>,
  };
}

export function createLiveAssistantTranscriptItems(run: StreamState): TranscriptItem[] {
  const hasAnswer = run.content.trim().length > 0;
  const processItem = createLiveAssistantProcessItem(run, { splitAnswer: hasAnswer });
  if (!hasAnswer) return [processItem];
  const processProps = processItem.props as unknown as AssistantProcessRenderProps;
  const hasProcess = Boolean(processProps.showPendingBubble)
    || (Array.isArray(processProps.timeline) && processProps.timeline.length > 0);
  const nodeId = run.targetNodeId || run.nodeId || null;
  const answerItem: TranscriptItem = {
    id: `live-${run.runId}-answer`,
    type: 'assistant_answer',
    conversation_id: run.conversationId,
    node_id: nodeId,
    anchor_node_id: run.anchorNodeId,
    run_id: run.runId,
    status: run.status,
    visibility: 'main',
    preview: run.content,
    props: {
      stream_status: run.status,
      stream_error_message: run.errorMessage,
    },
  };
  return hasProcess ? [processItem, answerItem] : [answerItem];
}

export type TimelineFoldBlock = {
  type: string;
  key: string;
};

export type AssistantFoldedContentBlock = {
  type: 'content';
  key: string;
  content: string;
};

type AssistantFoldSource = {
  content?: unknown;
  reasoning?: unknown;
  tool_interactions?: unknown[];
  tool_calls?: unknown[];
  tool_results?: unknown[];
};

export type TimelineFoldState<T extends TimelineFoldBlock> = {
  canFoldProcess: boolean;
  processBlocks: T[];
  contentBlocks: T[];
  visibleBlocks: T[];
};

export type StreamingTimelineFoldState<T extends TimelineFoldBlock> = TimelineFoldState<T> & {
  processExpanded: boolean;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
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

function getInteractionAssistantContent(interaction: unknown): string {
  const record = asRecord(interaction);
  const assistant = asRecord(record?.assistant);
  return typeof assistant?.content === 'string' ? assistant.content : '';
}

function interactionHasProcessHistory(interaction: unknown): boolean {
  const record = asRecord(interaction);
  const assistant = asRecord(record?.assistant);
  const reasoning = typeof record?.reasoning === 'string' ? record.reasoning : '';
  const toolCalls = Array.isArray(assistant?.tool_calls) ? assistant.tool_calls : [];
  const toolMessages = Array.isArray(record?.tools) ? record.tools : [];
  return reasoning.trim().length > 0 || toolCalls.length > 0 || toolMessages.length > 0;
}

export function formatProcessedDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return null;

  const totalSeconds = Math.max(1, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];

  if (hours > 0) parts.push(`${hours}h`);
  if (hours > 0 || minutes > 0) parts.push(`${minutes}m`);
  parts.push(`${seconds}s`);
  return parts.join(' ');
}

export function hasAssistantProcessHistory(message: AssistantFoldSource): boolean {
  const reasoning = typeof message.reasoning === 'string' ? message.reasoning : '';
  if (reasoning.trim().length > 0) return true;
  if (Array.isArray(message.tool_calls) && message.tool_calls.length > 0) return true;
  if (Array.isArray(message.tool_results) && message.tool_results.length > 0) return true;
  if (!Array.isArray(message.tool_interactions)) return false;
  return message.tool_interactions.some(interactionHasProcessHistory);
}

export function getAssistantFoldedContentBlocks(message: AssistantFoldSource): AssistantFoldedContentBlock[] {
  const interactions = Array.isArray(message.tool_interactions) ? message.tool_interactions : [];
  if (interactions.length === 0) {
    const content = typeof message.content === 'string' ? message.content : '';
    return content.trim() ? [{ type: 'content', key: 'content', content }] : [];
  }

  const interactionContents = interactions
    .map(getInteractionAssistantContent)
    .filter((content) => content.trim().length > 0);
  const finalContent = stripChronologicalPrefix(message.content, interactionContents);
  if (finalContent.trim()) {
    return [{ type: 'content', key: 'content-final', content: finalContent }];
  }

  return interactionContents.map((content, index) => ({
    type: 'content',
    key: `content-${index}`,
    content,
  }));
}

export function getTimelineFoldState<T extends TimelineFoldBlock>(
  blocks: T[],
  options: {
    processExpanded: boolean;
    finalContentKeys?: Iterable<string>;
    allowProcessOnly?: boolean;
  },
): TimelineFoldState<T> {
  const finalContentKeySet = options.finalContentKeys === undefined
    ? null
    : new Set(options.finalContentKeys);
  const isFinalContentBlock = (block: T) =>
    block.type === 'content' && (finalContentKeySet === null || finalContentKeySet.has(block.key));
  const processBlocks = blocks.filter((block) => !isFinalContentBlock(block));
  const contentBlocks = blocks.filter(isFinalContentBlock);
  const canFoldProcess = processBlocks.length > 0 && (contentBlocks.length > 0 || options.allowProcessOnly === true);

  if (!canFoldProcess) {
    return {
      canFoldProcess: false,
      processBlocks,
      contentBlocks,
      visibleBlocks: blocks,
    };
  }

  return {
    canFoldProcess: true,
    processBlocks,
    contentBlocks,
    visibleBlocks: options.processExpanded
      ? blocks
    : contentBlocks,
  };
}

export function getStreamingTimelineFoldState<T extends TimelineFoldBlock>(
  blocks: T[],
  finalContentKeys?: Iterable<string>,
  options?: {
    allowProcessOnly?: boolean;
  },
): StreamingTimelineFoldState<T> {
  return {
    ...getTimelineFoldState(blocks, {
      processExpanded: true,
      finalContentKeys,
      allowProcessOnly: options?.allowProcessOnly,
    }),
    processExpanded: true,
  };
}

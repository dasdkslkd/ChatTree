export type TimelineFoldBlock = {
  type: string;
  key: string;
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
  return parts.join('');
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
  const hasProcessBlocks = processBlocks.length > 0;
  const hasEmptyProcessShell = options.allowProcessOnly === true && blocks.length === 0;
  const canFoldProcess = (hasProcessBlocks || hasEmptyProcessShell)
    && (contentBlocks.length > 0 || options.allowProcessOnly === true);

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
    visibleBlocks: options.processExpanded ? blocks : contentBlocks,
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

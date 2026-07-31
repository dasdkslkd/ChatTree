import type {
  AssistantProcessBlock,
  AssistantProcessItem as AssistantProcessTranscriptItem,
} from '../../../types/transcript';
import type {
  AssistantProcessRenderProps,
  ToolRenderItem,
  ProcessRenderBlock,
} from './AssistantProcessTimeline';
import { AssistantProcessTimeline } from './AssistantProcessTimeline';
import { summarizeToolCall } from './toolCallFormatting';

interface AssistantProcessItemProps {
  item: AssistantProcessTranscriptItem;
}

function toolBlockToRenderItem(block: Extract<AssistantProcessBlock, { type: 'tool_call' }>): ToolRenderItem {
  const status = block.status === 'error' ? 'error' : block.status === 'running' ? 'running' : 'done';
  const outputText = block.result_preview || '';
  const argsText = block.args_preview || '';
  return {
    key: block.id,
    name: block.tool_name || 'tool',
    argsText,
    outputText,
    status,
    summary: summarizeToolCall(block.tool_name || 'tool', argsText, outputText, status),
  };
}

function timelineFromBlocks(item: AssistantProcessTranscriptItem): ProcessRenderBlock[] {
  const blocks = Array.isArray(item.blocks) ? item.blocks : [];
  const timeline: ProcessRenderBlock[] = [];
  let pendingTools: ToolRenderItem[] = [];

  const flushTools = () => {
    if (pendingTools.length === 0) return;
    timeline.push({
      type: 'tools',
      key: `tools:${pendingTools.map((tool) => tool.key).join(':')}`,
      items: pendingTools,
    });
    pendingTools = [];
  };

  const lastIndex = blocks.length - 1;
  for (let index = 0; index < blocks.length; index++) {
    const block = blocks[index];
    const isLast = index === lastIndex;
    if (block.type === 'tool_call') {
      pendingTools.push(toolBlockToRenderItem(block));
      continue;
    }
    flushTools();
    if (block.type === 'reasoning') {
      timeline.push({
        type: 'reasoning',
        key: block.id,
        reasoning: block.content || '',
        streaming: isLast && Boolean(block.streaming),
      });
    } else if (block.type === 'content') {
      timeline.push({
        type: 'content',
        key: block.id,
        content: block.content || '',
        streaming: isLast && Boolean(block.streaming),
      });
    }
  }
  flushTools();
  return timeline;
}

function getProcessProps(item: AssistantProcessTranscriptItem): AssistantProcessRenderProps {
  const timeline = timelineFromBlocks(item);
  const status = item.status || null;
  return {
    timeline,
    status,
    errorMessage: typeof item.message === 'string' ? item.message : null,
  };
}

export function AssistantProcessItem({ item }: AssistantProcessItemProps) {
  return <AssistantProcessTimeline item={item} props={getProcessProps(item)} />;
}

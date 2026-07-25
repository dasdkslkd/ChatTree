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
    summary: compactToolSummary(outputText || argsText || (status === 'running' ? '工具运行中' : '工具结果')),
  };
}

function compactToolSummary(text: string): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  return normalized.length > 100 ? `${normalized.slice(0, 97)}...` : normalized;
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

  for (const block of blocks) {
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
        streaming: Boolean(block.streaming),
      });
    } else if (block.type === 'content') {
      timeline.push({
        type: 'content',
        key: block.id,
        content: block.content || '',
        streaming: Boolean(block.streaming),
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
    duration: typeof item.duration_ms === 'number' ? item.duration_ms : 0,
    errorMessage: typeof item.message === 'string' ? item.message : null,
  };
}

export function AssistantProcessItem({ item }: AssistantProcessItemProps) {
  return <AssistantProcessTimeline item={item} props={getProcessProps(item)} />;
}

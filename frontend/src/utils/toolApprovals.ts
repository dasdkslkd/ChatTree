import type {
  ToolApprovalDecision,
  ToolApprovalPayload,
  ToolApprovalScope,
} from '../types/message';
import { getSlashRunLabel } from './slashRuntime';
import { formatToolArguments, summarizeToolCall } from './toolDisplay';

export interface ToolApprovalRunLike {
  runId: string;
  kind: string;
  pendingUserMessage?: string | null;
  pendingApprovals?: Record<string, ToolApprovalPayload | null | undefined> | null;
}

export interface PendingToolApprovalPrompt {
  approval: ToolApprovalPayload;
  runId: string;
  runKind: string;
  runLabel: string;
  sourceLabel: string;
  sourceSummary: string;
  toolSummary: string;
  argumentsText: string;
}

export type ToolApprovalDecisionHandler = (
  approvalId: string,
  decision: ToolApprovalDecision,
  scope: ToolApprovalScope,
  runId: string,
) => Promise<void>;

function compactSingleLine(value: string, limit: number): string {
  const singleLine = value.replace(/\s+/g, ' ').trim();
  if (singleLine.length <= limit) return singleLine;
  return `${singleLine.slice(0, limit - 3)}...`;
}

function getRunSourceSummary(run: ToolApprovalRunLike): string {
  const prompt = typeof run.pendingUserMessage === 'string' ? run.pendingUserMessage.trim() : '';
  if (prompt) return compactSingleLine(prompt, 120);
  return `${getSlashRunLabel(run.kind, run.pendingUserMessage)} · ${run.runId}`;
}

function getRunSourceLabel(run: ToolApprovalRunLike): string {
  if (run.kind === 'chat') return '主对话';
  if (run.kind === 'subagent') return 'Subagent';
  if (run.kind === 'workflow') return 'Workflow';
  if (run.kind === 'workflow_step') return 'Workflow 子任务';
  if (run.kind === 'side_question') return '侧边提问';
  if (run.kind === 'direct_response') return 'Slash';
  return getSlashRunLabel(run.kind, run.pendingUserMessage);
}

export function collectPendingToolApprovalPrompts(
  runs: ToolApprovalRunLike[],
  activeApprovalIds?: Set<string>,
): PendingToolApprovalPrompt[] {
  const prompts: PendingToolApprovalPrompt[] = [];
  const seen = new Set<string>();
  for (const run of runs) {
    for (const approval of Object.values(run.pendingApprovals ?? {})) {
      if (!approval || approval.status !== 'pending') continue;
      if (activeApprovalIds && !activeApprovalIds.has(approval.id)) continue;
      const key = `${run.runId}:${approval.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const toolName = approval.tool_name || 'tool';
      const rawArguments = approval.arguments_preview || '';
      prompts.push({
        approval,
        runId: run.runId,
        runKind: run.kind,
        runLabel: getSlashRunLabel(run.kind, run.pendingUserMessage),
        sourceLabel: getRunSourceLabel(run),
        sourceSummary: getRunSourceSummary(run),
        toolSummary: summarizeToolCall(toolName, rawArguments) || approval.reason || `调用 ${toolName}`,
        argumentsText: rawArguments ? formatToolArguments(rawArguments) : '',
      });
    }
  }
  return prompts;
}

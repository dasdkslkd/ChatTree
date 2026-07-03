import type { SlashCommandInfo } from '../types/slash';

export interface SlashCompletionState {
  active: boolean;
  query: string;
  args: string;
}

function slashCommandNames(command: Pick<SlashCommandInfo, 'name' | 'aliases'>): string[] {
  return [command.name, ...(command.aliases || [])];
}

export function getSlashCompletionState(text: string): SlashCompletionState {
  if (!text.startsWith('/')) return { active: false, query: '', args: '' };
  const body = text.slice(1);
  const match = body.match(/^([A-Za-z0-9_.:-]*)(?:\s+([\s\S]*))?$/);
  if (!match) return { active: false, query: '', args: '' };
  return { active: true, query: match[1] || '', args: match[2] || '' };
}

export function getSlashCompletionCandidates(
  text: string,
  commands: SlashCommandInfo[],
  limit = 6,
): SlashCommandInfo[] {
  const state = getSlashCompletionState(text);
  if (!state.active) return [];
  if (/\s/.test(text.slice(1))) return [];
  const query = state.query.toLowerCase();
  return commands
    .filter((command) => command.enabled)
    .filter((command) => slashCommandNames(command).some((name) => name.toLowerCase().startsWith(query)))
    .slice(0, limit);
}

export function applySlashCommandCompletion(text: string, command: Pick<SlashCommandInfo, 'name'>): string {
  const state = getSlashCompletionState(text);
  const suffix = state.args ? ` ${state.args}` : ' ';
  return `/${command.name}${suffix}`;
}

export interface QueueDecisionInput {
  currentBranchHasStreamingChat: boolean;
  slashCommand?: Pick<SlashCommandInfo, 'blocks_main_thread'> | null;
}

export function shouldQueueForMainThread({
  currentBranchHasStreamingChat,
  slashCommand,
}: QueueDecisionInput): boolean {
  if (!currentBranchHasStreamingChat) return false;
  return slashCommand?.blocks_main_thread ?? true;
}

export interface RunDraftLike {
  kind: string;
  status: string;
  pendingUserMessage?: string | null;
  content?: string | null;
  reasoning?: string | null;
  toolInteractions?: unknown[] | null;
  workflowEvents?: unknown[] | null;
  command?: { stdout?: string | null; stderr?: string | null; events?: unknown[] | null } | null;
  pendingApprovals?: Record<string, { status?: string | null } | null | undefined> | null;
  metadata?: Record<string, unknown> | null;
}

export function shouldRenderRunDraft(run: RunDraftLike): boolean {
  if (run.kind === 'chat' || run.kind === 'side_question' || run.kind === 'direct_response') return true;
  if (
    run.kind === 'command'
    && ['completed', 'failed', 'cancelled', 'stopped', 'error'].includes(run.status)
    && run.metadata?.command_notification_state === 'observed'
  ) {
    return false;
  }
  if (run.kind === 'command' && (run.status === 'streaming' || run.status === 'stopping')) return true;
  if (run.kind === 'subagent' && (run.status === 'streaming' || run.status === 'waiting_approval' || run.status === 'stopping')) return true;
  if (run.pendingUserMessage) return true;
  if (run.status === 'error' || run.status === 'stopped' || run.status === 'stopping') return true;
  if (run.content || run.reasoning) return true;
  if (Array.isArray(run.toolInteractions) && run.toolInteractions.length > 0) return true;
  if (Array.isArray(run.workflowEvents) && run.workflowEvents.length > 0) return true;
  if (run.command?.stdout || run.command?.stderr) return true;
  if (Array.isArray(run.command?.events) && run.command.events.length > 0) return true;
  if (run.pendingApprovals && Object.values(run.pendingApprovals).some((approval) => approval?.status === 'pending')) return true;
  return false;
}

export function getSlashRunLabel(kind: string, pendingUserMessage?: string | null): string {
  if (kind === 'side_question') return 'btw';
  if (kind === 'subagent') return 'fork';
  if (kind === 'command') return 'command';
  if (kind === 'workflow') return 'workflow';
  if (kind === 'direct_response') {
    const match = pendingUserMessage?.match(/^\s*\/(status|help|capabilities)\b/i);
    return match ? match[1].toLowerCase() : 'status/help/capabilities';
  }
  return kind;
}
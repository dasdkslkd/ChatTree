import type { SlashCommandInfo } from '../types/slash';
import type { TreeData, TreeNode } from '../api/conversation';
import { stripFileMention } from './fileMention';

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
  limit?: number,
): SlashCommandInfo[] {
  const state = getSlashCompletionState(text);
  if (!state.active) return [];
  if (/\s/.test(text.slice(1))) return [];
  const query = state.query.toLowerCase();
  const matches = commands
    .filter((command) => command.enabled)
    .filter((command) => slashCommandNames(command).some((name) => name.toLowerCase().startsWith(query)));
  return typeof limit === 'number' ? matches.slice(0, limit) : matches;
}

export function applySlashCommandCompletion(text: string, command: Pick<SlashCommandInfo, 'name'>): string {
  const state = getSlashCompletionState(text);
  const suffix = state.args ? ` ${state.args}` : ' ';
  return `/${command.name}${suffix}`;
}

export interface ReferNodeCompletionState {
  active: boolean;
  query: string;
  replaceStart: number;
  replaceEnd: number;
}

export interface ReferNodeSuggestion {
  id: string;
  insertText: string;
  userPreview: string;
  assistantPreview: string;
  modelId: string | null;
  isCurrent: boolean;
  isOnCurrentBranch: boolean;
  hasPruneSummary: boolean;
  timestamp: number;
}

const REFER_SELECTOR_PATTERN = /^(?:node|compact|before|prune|truncated):\S+$/i;

function compactPreview(value: string | null | undefined, maxLength: number): string {
  const compact = (value || '').replace(/\s+/g, ' ').trim();
  if (compact.length <= maxLength) return compact;
  return `${compact.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;
}


function getTreeUserContent(node: Pick<TreeNode, 'user_content'>): string {
  return node.user_content ?? '';
}

function isReferSelectorToken(token: string): boolean {
  return REFER_SELECTOR_PATTERN.test(token);
}

export function getReferNodeCompletionState(text: string): ReferNodeCompletionState {
  const commandMatch = text.match(/^\/refer\b/);
  if (!commandMatch) return { active: false, query: '', replaceStart: 0, replaceEnd: 0 };

  const commandEnd = commandMatch[0].length;
  if (text.length === commandEnd || !/\s/.test(text[commandEnd] || '')) {
    return { active: false, query: '', replaceStart: 0, replaceEnd: 0 };
  }

  const whitespaceMatch = text.slice(commandEnd).match(/^\s+/);
  const argsStart = commandEnd + (whitespaceMatch?.[0].length || 0);
  const args = text.slice(argsStart);
  const lastTokenStartInArgs = Math.max(
    args.lastIndexOf(' '),
    args.lastIndexOf('\n'),
    args.lastIndexOf('\t'),
  ) + 1;
  const prefix = args.slice(0, lastTokenStartInArgs);
  const currentToken = args.slice(lastTokenStartInArgs);
  const previousTokens = prefix.trim().split(/\s+/).filter(Boolean);

  if (previousTokens.some((token) => !isReferSelectorToken(token))) {
    return { active: false, query: '', replaceStart: 0, replaceEnd: 0 };
  }

  if (currentToken && !currentToken.toLowerCase().startsWith('node:')) {
    return { active: false, query: '', replaceStart: 0, replaceEnd: 0 };
  }

  return {
    active: true,
    query: currentToken.toLowerCase().startsWith('node:') ? currentToken.slice(5) : currentToken,
    replaceStart: argsStart + lastTokenStartInArgs,
    replaceEnd: text.length,
  };
}

function currentBranchRanks(treeData: TreeData): Map<string, number> {
  const byId = new Map(treeData.nodes.map((node) => [node.id, node]));
  const ranks = new Map<string, number>();
  let node: TreeNode | undefined = byId.get(treeData.current_node_id);
  let rank = 0;
  while (node && !ranks.has(node.id)) {
    ranks.set(node.id, rank);
    rank += 1;
    node = node.parent_id ? byId.get(node.parent_id) : undefined;
  }
  return ranks;
}

export function getReferNodeCompletionCandidates(
  text: string,
  treeData: TreeData | null,
): ReferNodeSuggestion[] {
  const state = getReferNodeCompletionState(text);
  if (!state.active || !treeData) return [];

  const query = state.query.trim().toLowerCase();
  const branchRanks = currentBranchRanks(treeData);
  const selectedNodeIds = new Set(
    Array.from(text.slice(0, state.replaceStart).matchAll(/\bnode:(\S+)/gi))
      .flatMap((match) => (match[1] ? [match[1].toLowerCase()] : [])),
  );

  return treeData.nodes
    .filter((node) => getTreeUserContent(node).trim() || node.assistant_content.trim())
    .filter((node) => !selectedNodeIds.has(node.id.toLowerCase()))
    .filter((node) => !query || node.id.toLowerCase().includes(query))
    .map((node) => {
      const userContent = compactPreview(stripFileMention(getTreeUserContent(node)), 96);
      return {
        id: node.id,
        insertText: `node:${node.id}`,
        userPreview: userContent,
        assistantPreview: compactPreview(node.assistant_content, 120),
        modelId: node.model_id,
        isCurrent: node.id === treeData.current_node_id || node.is_current,
        isOnCurrentBranch: branchRanks.has(node.id),
        hasPruneSummary: false,
        timestamp: node.timestamp,
      };
    })
    .sort((a, b) => {
      if (a.isCurrent !== b.isCurrent) return a.isCurrent ? -1 : 1;
      const aRank = branchRanks.get(a.id);
      const bRank = branchRanks.get(b.id);
      if (aRank != null && bRank != null) return aRank - bRank;
      if (aRank != null) return -1;
      if (bRank != null) return 1;
      return b.timestamp - a.timestamp;
    });
}

export function applyReferNodeCompletion(text: string, suggestion: Pick<ReferNodeSuggestion, 'insertText'>): string {
  const state = getReferNodeCompletionState(text);
  if (!state.active) return text;
  return `${text.slice(0, state.replaceStart)}${suggestion.insertText} ${text.slice(state.replaceEnd)}`;
}

export interface QueueDecisionInput {
  currentBranchHasStreamingChat: boolean;
  slashCommand?: Pick<SlashCommandInfo, 'blocks_main_thread' | 'dispatch_kind'> | null;
}

export function shouldQueueForMainThread({
  currentBranchHasStreamingChat,
  slashCommand,
}: QueueDecisionInput): boolean {
  if (!currentBranchHasStreamingChat) return false;
  if (slashCommand?.dispatch_kind === 'refer_prompt') return false;
  return slashCommand?.blocks_main_thread ?? true;
}

export interface RunDraftLike {
  kind: string;
  status: string;
  pendingUserMessage?: string | null;
  content?: string | null;
  reasoning?: string | null;
  workflowEvents?: unknown[] | null;
  command?: { stdout?: string | null; stderr?: string | null; events?: unknown[] | null } | null;
  metadata?: Record<string, unknown> | null;
}

export function shouldRenderRunDraft(run: RunDraftLike): boolean {
  if (run.kind === 'chat' || run.kind === 'side_question' || run.kind === 'direct_response') return true;
  if (run.kind === 'command') {
    return run.metadata?.shell_auto_backgrounded === true
      && typeof run.metadata?.result_observed_at === 'undefined';
  }
  if (run.kind === 'subagent' && (run.status === 'streaming' || run.status === 'waiting_approval' || run.status === 'stopping')) return true;
  if ((run.kind === 'workflow' || run.kind === 'workflow_step') && (run.status === 'streaming' || run.status === 'waiting_approval' || run.status === 'stopping')) return true;
  if (run.pendingUserMessage) return true;
  if (run.status === 'error' || run.status === 'stopped' || run.status === 'stopping') return true;
  if (run.content || run.reasoning) return true;
  if (Array.isArray(run.workflowEvents) && run.workflowEvents.length > 0) return true;
  if (run.command?.stdout || run.command?.stderr) return true;
  if (Array.isArray(run.command?.events) && run.command.events.length > 0) return true;
  return false;
}

export function getSlashRunLabel(kind: string, pendingUserMessage?: string | null): string {
  if (kind === 'side_question') return 'btw';
  if (kind === 'subagent') return 'fork';
  if (kind === 'command') return 'command';
  if (kind === 'workflow') return 'workflow';
  if (kind === 'direct_response') {
    const match = pendingUserMessage?.match(/^\s*\/(status|help|capabilities|prune-summary|prune)\b/i);
    return match ? match[1].toLowerCase() : 'status/help/capabilities';
  }
  return kind;
}

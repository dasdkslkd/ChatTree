import { slashApi } from '../api/slash';
import {
  StaleConnectionEpochError,
  captureConnectionEpoch,
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';
import type { SlashCommandInfo } from '../types/slash';

const DEFAULT_COMMANDS: SlashCommandInfo[] = [
  {
    name: 'init',
    aliases: [],
    description: 'create project agent instructions',
    supports_inline_args: false,
    requires_args: false,
    dispatch_kind: 'main_prompt',
    tool_policy: 'inherit',
    persistence_policy: 'main_thread',
    run_kind: 'chat',
    stream_target_policy: 'target_node',
    blocks_main_thread: true,
    enabled: true,
  },
  {
    name: 'review',
    aliases: [],
    description: 'review current changes or custom target',
    supports_inline_args: true,
    requires_args: false,
    dispatch_kind: 'main_prompt',
    tool_policy: 'inherit',
    persistence_policy: 'main_thread',
    run_kind: 'chat',
    stream_target_policy: 'target_node',
    blocks_main_thread: true,
    enabled: true,
  },
  {
    name: 'refer',
    aliases: [],
    description: 'continue this turn with referenced historical evidence',
    supports_inline_args: true,
    requires_args: true,
    usage_args_label: 'selector... 本轮问题或指令',
    dispatch_kind: 'refer_prompt',
    tool_policy: 'inherit',
    persistence_policy: 'main_thread',
    run_kind: 'chat',
    stream_target_policy: 'target_node',
    blocks_main_thread: true,
    enabled: true,
  },
  {
    name: 'btw',
    aliases: [],
    description: 'ask a side question without interrupting the main conversation',
    supports_inline_args: true,
    requires_args: true,
    dispatch_kind: 'side_question',
    tool_policy: 'disabled',
    persistence_policy: 'side_run',
    run_kind: 'side_question',
    stream_target_policy: 'anchor_only',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'fork',
    aliases: [],
    description: 'start a background fork',
    supports_inline_args: true,
    requires_args: true,
    dispatch_kind: 'subagent',
    tool_policy: 'inherit',
    persistence_policy: 'background_run',
    run_kind: 'subagent',
    stream_target_policy: 'anchor_only',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'workflow',
    aliases: [],
    description: 'run a dynamic workflow',
    supports_inline_args: true,
    requires_args: true,
    dispatch_kind: 'workflow',
    tool_policy: 'inherit',
    persistence_policy: 'background_run',
    run_kind: 'workflow',
    stream_target_policy: 'anchor_only',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'status',
    aliases: [],
    description: 'show current runtime status',
    supports_inline_args: false,
    requires_args: false,
    dispatch_kind: 'direct_response',
    tool_policy: 'disabled',
    persistence_policy: 'side_run',
    run_kind: 'direct_response',
    stream_target_policy: 'none',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'help',
    aliases: [],
    description: 'show available slash commands',
    supports_inline_args: false,
    requires_args: false,
    dispatch_kind: 'direct_response',
    tool_policy: 'disabled',
    persistence_policy: 'side_run',
    run_kind: 'direct_response',
    stream_target_policy: 'none',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'capabilities',
    aliases: [],
    description: 'show available capabilities',
    supports_inline_args: false,
    requires_args: false,
    dispatch_kind: 'direct_response',
    tool_policy: 'disabled',
    persistence_policy: 'side_run',
    run_kind: 'direct_response',
    stream_target_policy: 'none',
    blocks_main_thread: false,
    enabled: true,
  },
  {
    name: 'prune-summary',
    aliases: ['prune'],
    description: 'summarize child branches under the current or specified node',
    supports_inline_args: true,
    requires_args: false,
    usage_args_label: 'node:<节点ID> 可选引导',
    dispatch_kind: 'direct_response',
    tool_policy: 'disabled',
    persistence_policy: 'side_run',
    run_kind: 'direct_response',
    stream_target_policy: 'anchor_only',
    blocks_main_thread: false,
    enabled: true,
  },
];

let commands = DEFAULT_COMMANDS;
let refreshPromise: Promise<SlashCommandInfo[]> | null = null;
let refreshToken: ConnectionEpochToken | null = null;

function sameEpochToken(
  left: ConnectionEpochToken | null,
  right: ConnectionEpochToken,
): boolean {
  return Boolean(left
    && left.generation === right.generation
    && left.profileId === right.profileId
    && left.serverInstanceId === right.serverInstanceId
    && left.connectionEpoch === right.connectionEpoch
    && left.connectionLeaseId === right.connectionLeaseId);
}

function isStaleEpoch(error: unknown, token: ConnectionEpochToken | null): boolean {
  return !token
    || error instanceof StaleConnectionEpochError
    || !connectionEpochRuntime.isCurrent(token);
}

function commandNames(command: SlashCommandInfo): string[] {
  return [command.name, ...(command.aliases || [])];
}

export interface SlashCommandMatch {
  command: SlashCommandInfo;
  inputName: string;
  args: string;
}

export const slashRegistry = {
  list: (): SlashCommandInfo[] => commands,

  refresh: async (): Promise<SlashCommandInfo[]> => {
    let token: ConnectionEpochToken | null = null;
    try {
      token = captureConnectionEpoch();
      if (refreshPromise && sameEpochToken(refreshToken, token)) {
        return refreshPromise;
      }

      const promise = slashApi.listCommands()
        .then((next) => {
          connectionEpochRuntime.assertCurrent(token!);
          commands = next.filter((command) => command.enabled);
          return commands;
        })
        .catch((error: unknown) => {
          if (isStaleEpoch(error, token)) return commands;
          throw error;
        })
        .finally(() => {
          if (refreshPromise !== promise) return;
          refreshPromise = null;
          refreshToken = null;
        });
      refreshToken = token;
      refreshPromise = promise;
      return promise;
    } catch (error) {
      if (isStaleEpoch(error, token)) return commands;
      throw error;
    }
  },

  match: (text: string): SlashCommandMatch | null => {
    const match = text.match(/^\s*\/([A-Za-z0-9_.:-]+)(?:\s+([\s\S]*))?$/);
    if (!match) return null;
    const inputName = match[1];
    const args = match[2] || '';
    const command = commands.find((candidate) => commandNames(candidate).includes(inputName));
    if (!command?.enabled) return null;
    if (args && !command.supports_inline_args) return null;
    return { command, inputName, args };
  },
};

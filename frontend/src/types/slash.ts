export type SlashDispatchKind =
  | 'main_prompt'
  | 'side_question'
  | 'subagent'
  | 'workflow'
  | 'direct_response'
  | 'local_ui';

export type SlashToolPolicy = 'inherit' | 'disabled' | 'read_only';
export type SlashPersistencePolicy = 'main_thread' | 'side_run' | 'background_run' | 'none';
export type SlashStreamTargetPolicy = 'target_node' | 'anchor_only' | 'none';

export interface SlashCommandInfo {
  name: string;
  aliases: string[];
  description: string;
  supports_inline_args: boolean;
  requires_args: boolean;
  usage_args_label?: string;
  dispatch_kind: SlashDispatchKind;
  tool_policy: SlashToolPolicy;
  persistence_policy: SlashPersistencePolicy;
  run_kind: string | null;
  stream_target_policy: SlashStreamTargetPolicy;
  blocks_main_thread: boolean;
  enabled: boolean;
}

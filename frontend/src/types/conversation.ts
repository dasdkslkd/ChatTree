export interface WorkspaceContext {
  cwd: string;
  workspace_roots: string[];
  protected_paths?: string[];
  label?: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  model: string;
  model_id: string;
  provider_id: string;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
  multi_agent_mode?: MultiAgentMode;
  current_node_id: string;
  workspace?: WorkspaceContext;
  total_tokens: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

export type MultiAgentMode = 'none' | 'explicit_request_only' | 'proactive';

export interface ConversationCreateRequest {
  title?: string;
  prompt_id?: string;
  prompt_mode?: 'override' | 'append';
  workspace?: WorkspaceContext;
  multi_agent_mode?: MultiAgentMode;
}

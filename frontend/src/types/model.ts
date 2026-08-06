// 推理强度声明（levels 既是 UI 选项，也是合法档位）
export interface ReasoningEffortSpec {
  levels: string[];
  default?: string | null;
}

// 思考模式开关声明
export interface ThinkingSpec {
  toggleable: boolean;
  default_enabled: boolean;
}

// 单个模型的统一能力声明（reasoning_effort/thinking 为 null = 不显示对应控件）
export interface ModelMetadata {
  model_id: string;
  route_id: string;
  protocol: 'openai_chat_completions' | 'openai_responses' | 'anthropic_messages' | 'gemini_generate_content';
  endpoint: string;
  context_length?: number | null;
  supports_vision?: boolean;
  supports_tools?: boolean;
  reasoning_effort?: ReasoningEffortSpec | null;
  thinking?: ThinkingSpec | null;
}

// 提供商 ID（动态字符串，不再使用枚举）
export type ModelProvider = string;
export type ContextWindowLimit = 200000 | 400000 | 600000 | null;

// 订阅登录信息（与 api_key 二选一）
export interface AuthInfo {
  type?: 'oauth' | 'api';
  subscription?: 'codex' | 'copilot' | 'claude' | 'xai';
  access?: string;
  refresh?: string;
  expires?: number;
  account_id?: string;
  account_name?: string;
  account_email?: string;
  enterprise_domain?: string;
}

// 单个提供商配置
export interface ModelProviderConfig {
  name: string;
  models: string[];
  api_key: string;
  base_url: string;
  organization?: string;
  project?: string;
  hidden_models: string[];
  enabled: boolean;
  auth?: AuthInfo;
  models_url_override?: string;
  custom_user_agent?: string;
  // 反向代理来源标识：'reverse_proxy' 表示由 launcher 通过 SSH 反向隧道注入
  source?: 'reverse_proxy';
}

export type McpTransport = 'streamable_http' | 'stdio';

export interface McpServerConfig {
  enabled: boolean;
  transport: McpTransport;
  url?: string;
  endpoint?: string;
  bearer_token?: string;
  headers?: Record<string, string>;
  command?: string | string[];
  args?: string[];
  stdio_framing?: 'content_length' | 'jsonl';
  env?: Record<string, string>;
  cwd?: string;
  timeout?: number;
  startup_timeout?: number;
  tool_call_timeout?: number;
  heartbeat_enabled?: boolean;
  heartbeat_interval?: number;
  auto_start?: boolean;
  auto_reconnect?: boolean;
  max_reconnect_attempts?: number;
  http_retries?: number;
  http_retry_backoff?: number;
  enabled_tools?: string[] | null;
  disabled_tools?: string[];
}

export type BuiltinToolExposure = 'minimal' | 'coding' | 'full';

export interface BuiltinCodeToolsConfig {
  enabled?: boolean;
  workspace_roots?: string[];
  protected_paths?: string[];
  command_timeout_seconds?: number;
  shell_initial_wait_seconds?: number;
  max_read_chars?: number;
  max_output_chars?: number;
}

export interface BuiltinToolsConfig {
  enabled?: boolean;
  exposure?: BuiltinToolExposure;
  model_visible_tools?: string[];
  hidden_tools?: string[];
  code?: BuiltinCodeToolsConfig;
}

export interface WebSearchConfig {
  enabled?: boolean;
  searxng?: {
    searxng_url?: string;
    engines?: string;
    language?: string;
    max_results?: number;
    timeout?: number;
  };
}

export interface ToolsConfig {
  enabled?: boolean;
  max_result_length?: number;
  default_permission_mode?: 'auto_approve' | 'modify_only' | 'ask_always' | 'plan';
  enabled_tools?: string[] | null;
  disabled_tools?: string[];
  wait_agent_timeout_seconds?: number;
  builtin?: BuiltinToolsConfig;
  web_search?: WebSearchConfig;
  fetch_url?: Record<string, unknown>;
  mcp?: {
    enabled?: boolean;
    servers?: Record<string, McpServerConfig>;
  };
}

export interface BuiltinWebStatus {
  enabled: boolean;
  searxng_url: string;
  available: boolean;
  status_code?: number | null;
  error?: string | null;
}

export interface McpServerStatus {
  name: string;
  enabled: boolean;
  transport?: McpTransport;
  auto_start?: boolean;
  connected: boolean;
  tools_count?: number;
  error?: string | null;
  source?: 'user' | 'plugin' | string;
  plugin_id?: string | null;
  plugin_name?: string | null;
}

export interface McpToolStatus {
  server: string;
  name: string;
  callable_name: string;
}

export interface ToolInventoryStatus {
  tools_enabled: boolean;
  model_visible_tools: string[];
  local_tools: string[];
  hidden_local_tools?: string[];
  mcp_servers: McpServerStatus[];
  mcp_tools: McpToolStatus[];
}

export type CapabilityKind = 'skill' | 'agent' | 'plugin' | 'mcp_server' | 'hook';
export type CapabilitySource = 'system' | 'user' | 'project' | 'plugin';

export interface CapabilitySkill {
  name: string;
  kind: CapabilityKind;
  source: CapabilitySource | string;
  description?: string;
  path?: string | null;
  plugin_id?: string | null;
  plugin_name?: string | null;
  namespace?: string | null;
  when_to_use?: string | null;
  allowed_tools?: string[];
  model?: string | null;
  metadata?: Record<string, unknown>;
}

export interface CapabilityAgent {
  name: string;
  description?: string;
  system_prompt?: string;
  tools?: string[];
  skills?: string[];
  model?: string | null;
  max_turns?: number | null;
  plugin_id?: string | null;
  plugin_name?: string | null;
  path?: string | null;
  source: CapabilitySource | string;
  metadata?: Record<string, unknown>;
}

export interface CapabilityPlugin {
  plugin_id: string;
  name: string;
  root: string;
  enabled: boolean;
  description?: string;
  version?: string | null;
  skill_roots?: string[];
  agent_roots?: string[];
  hooks?: string[];
  mcp_servers?: Record<string, unknown>;
  interface?: Record<string, unknown>;
  error?: string | null;
}

export interface CapabilityInventory {
  skills: CapabilitySkill[];
  agents: CapabilityAgent[];
  plugins: CapabilityPlugin[];
}

export interface DevEnvironmentConfig {
  tools?: Record<string, string>;
  environments?: Record<string, string>;
  default_environment?: string;
}

export interface ProjectCapabilityConfig {
  label?: string;
  visible?: boolean;
  enabled_skills?: string[] | null;
  enabled_mcp_servers?: string[] | null;
  enabled_agents?: string[] | null;
  dev_environment?: DevEnvironmentConfig;
}

export interface ProjectSettingsItem {
  path: string;
  label: string;
  workspace: {
    cwd: string;
    workspace_roots: string[];
    protected_paths?: string[];
    label?: string;
  };
  conversation_count: number;
  latest_updated_at: number;
  config: ProjectCapabilityConfig;
}

export interface ProjectSettingsResponse {
  projects: ProjectSettingsItem[];
  config: Record<string, ProjectCapabilityConfig>;
}

// 完整配置数据
export interface ConfigData {
  default_provider: string;
  default_model: string;
  /** 默认模型的推理强度档位（null=未配置，按模型元数据默认） */
  default_reasoning_effort?: string | null;
  /** 默认模型的思考模式开关（null=未配置，按模型元数据默认） */
  default_thinking_enabled?: boolean | null;
  context_window: ContextWindowLimit;
  model_transport: ModelTransportConfig;
  provider: Record<string, ModelProviderConfig>;
  tools?: ToolsConfig;
  dev_environment?: DevEnvironmentConfig;
  projects?: Record<string, ProjectCapabilityConfig>;
}

// 配置更新请求
export interface ConfigUpdateRequest {
  default_provider?: string;
  default_model?: string;
  default_reasoning_effort?: string | null;
  default_thinking_enabled?: boolean | null;
  context_window?: ContextWindowLimit;
  model_transport?: Partial<ModelTransportConfig>;
  provider_configs?: Record<string, Partial<ModelProviderConfig>>;
  tools?: ToolsConfig;
  dev_environment?: DevEnvironmentConfig;
  projects?: Record<string, ProjectCapabilityConfig>;
}

export interface ModelTransportConfig {
  connect_timeout_seconds: number;
  first_event_timeout_seconds: number;
  stream_idle_timeout_seconds: number;
  sse_heartbeat_seconds: number;
  max_request_retries: number;
  max_stream_retries: number;
  retry_base_delay_seconds: number;
  retry_max_delay_seconds: number;
  retry_jitter_fraction: number;
}

// 添加提供商请求
export interface AddProviderRequest {
  id: string;
  name: string;
  base_url?: string;
  api_key?: string;
  auth?: AuthInfo;
}

// API 格式类型
export type APIFormat = 'chat_completions' | 'responses' | 'anthropic' | 'gemini';

// 提供商 ID（动态字符串，不再使用枚举）
export type ModelProvider = string;

// 单个提供商配置
export interface ModelProviderConfig {
  name: string;
  models: string[];
  api_key: string;
  base_url: string;
  organization?: string;
  project?: string;
  api_format: APIFormat;
  hidden_models: string[];
  enabled: boolean;
  default_model: string;
}

// 完整配置数据
export interface ConfigData {
  default_provider: string;
  provider: Record<string, ModelProviderConfig>;
}

// 配置更新请求
export interface ConfigUpdateRequest {
  default_provider?: string;
  provider_configs?: Record<string, Partial<ModelProviderConfig>>;
}

// 添加提供商请求
export interface AddProviderRequest {
  id: string;
  name: string;
  api_format: APIFormat;
  base_url?: string;
  api_key?: string;
}

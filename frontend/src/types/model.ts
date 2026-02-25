// 模型提供商枚举
export type ModelProvider = 
  | 'openai' 
  | 'azure' 
  | 'gemini' 
  | 'ollama' 
  | 'deepseek' 
  | 'anthropic' 
  | 'groq' 
  | 'local'
  | 'nvidia';

// 单个提供商配置
export interface ModelProviderConfig {
  name: string;
  models: string[];
  api_key: string;
  base_url: string;
  organization?: string;
  project?: string;
  enabled: boolean;
  default_model: string;
  is_async?: boolean;
}

// 完整配置数据
export interface ConfigData {
  default_provider: ModelProvider;
  provider: Record<ModelProvider, ModelProviderConfig>;
}

// 配置更新请求
export interface ConfigUpdateRequest {
  default_provider?: ModelProvider;
  provider_configs?: Partial<Record<ModelProvider, Partial<ModelProviderConfig>>>;
}

// 旧的类型定义（兼容）
export interface ModelConfig {
  api_key?: string;
  base_url?: string;
  model?: string;
  [key: string]: any;
}

export interface ProviderConfig {
  default: string;
  models: Record<string, ModelConfig>;
}
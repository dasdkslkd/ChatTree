import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Save, RefreshCw, X, Download, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '../api/config';
import { modelApi } from '../api/model';
import type { ConfigData, ModelProvider, ModelProviderConfig } from '../types/model';

// 所有支持的提供商
const ALL_PROVIDERS: ModelProvider[] = [
  'openai', 'ollama', 'deepseek', 'gemini', 'groq', 'azure', 'anthropic', 'nvidia', 'local',
];

// 提供商显示名称和描述
const PROVIDER_INFO: Record<ModelProvider, { label: string; description: string }> = {
  openai: { label: 'OpenAI', description: 'GPT-4, GPT-3.5 等模型' },
  ollama: { label: 'Ollama', description: '本地运行的开源模型' },
  deepseek: { label: 'DeepSeek', description: 'DeepSeek AI 模型' },
  gemini: { label: 'Google Gemini', description: 'Google Gemini 系列模型' },
  groq: { label: 'Groq', description: '超快推理的 LLM' },
  azure: { label: 'Azure OpenAI', description: '微软 Azure 托管的 OpenAI' },
  anthropic: { label: 'Anthropic', description: 'Claude 系列模型' },
  nvidia: { label: 'NVIDIA NIM', description: 'NVIDIA AI 推理平台' },
  local: { label: 'Local', description: '自定义本地模型' },
};

// 默认提供商配置
const DEFAULT_PROVIDER_CONFIG: ModelProviderConfig = {
  name: '',
  models: [],
  api_key: '',
  base_url: '',
  organization: '',
  project: '',
  enabled: false,
  default_model: '',
};

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newModelInputs, setNewModelInputs] = useState<Record<ModelProvider, string>>({} as Record<ModelProvider, string>);
  const [fetchingModels, setFetchingModels] = useState<Record<ModelProvider, boolean>>({} as Record<ModelProvider, boolean>);

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
    } catch (err) {
      toast.error('加载配置失败: ' + (err instanceof Error ? err.message : '未知错误'));
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!config) return;
    try {
      setSaving(true);
      await configApi.update({
        default_provider: config.default_provider,
        provider_configs: config.provider,
      } as any);
      toast.success('配置保存成功！');
    } catch (err) {
      toast.error('保存配置失败: ' + (err instanceof Error ? err.message : '未知错误'));
      console.error(err);
    } finally {
      setSaving(false);
    }
  };

  const handleDefaultProviderChange = (provider: ModelProvider) => {
    if (!config) return;
    setConfig({ ...config, default_provider: provider });
  };

  const getProviderConfig = (provider: ModelProvider): ModelProviderConfig => {
    if (!config?.provider?.[provider]) {
      return { ...DEFAULT_PROVIDER_CONFIG, name: provider };
    }
    return config.provider[provider];
  };

  const updateProviderConfig = (provider: ModelProvider, updates: Partial<ModelProviderConfig>) => {
    if (!config) return;
    const newConfig = { ...config };
    const currentConfig = getProviderConfig(provider);
    newConfig.provider = {
      ...newConfig.provider,
      [provider]: { ...currentConfig, ...updates },
    };
    setConfig(newConfig);
  };

  const handleAddModel = (provider: ModelProvider) => {
    const modelName = newModelInputs[provider]?.trim();
    if (!modelName) return;

    const currentConfig = getProviderConfig(provider);
    const models = [...(currentConfig.models || [])];
    if (!models.includes(modelName)) {
      models.push(modelName);
      updateProviderConfig(provider, { models });
    }
    setNewModelInputs({ ...newModelInputs, [provider]: '' });
  };

  const handleRemoveModel = (provider: ModelProvider, modelName: string) => {
    const currentConfig = getProviderConfig(provider);
    const models = (currentConfig.models || []).filter(m => m !== modelName);
    const updates: Partial<ModelProviderConfig> = { models };
    if (currentConfig.default_model === modelName) {
      updates.default_model = models[0] || '';
    }
    updateProviderConfig(provider, updates);
  };

  const fetchModelsFromProvider = async (provider: ModelProvider) => {
    try {
      setFetchingModels({ ...fetchingModels, [provider]: true });
      const models = await modelApi.list(provider);
      if (models && models.length > 0) {
        updateProviderConfig(provider, { models });
        toast.success(`成功获取 ${models.length} 个模型`);
      } else {
        toast.error('未获取到模型列表，请检查 API 配置是否正确');
      }
    } catch (err) {
      toast.error('获取模型列表失败: ' + (err instanceof Error ? err.message : '未知错误'));
      console.error(err);
    } finally {
      setFetchingModels({ ...fetchingModels, [provider]: false });
    }
  };

  const getEnabledProviders = (): ModelProvider[] => {
    if (!config) return [];
    return ALL_PROVIDERS.filter(p => config.provider?.[p]?.enabled);
  };

  const renderProviderConfig = (provider: ModelProvider) => {
    const providerConfig = getProviderConfig(provider);
    const info = PROVIDER_INFO[provider];
    const models = providerConfig.models || [];

    return (
      <AccordionItem value={provider} key={provider}>
        <AccordionTrigger>
          <div className="flex items-center gap-3 w-full">
            <span className="font-semibold flex-1 text-left">{info.label}</span>
            {providerConfig.enabled && (
              <Badge variant="secondary" className="bg-green-100 text-green-700 text-xs">
                已启用
              </Badge>
            )}
          </div>
        </AccordionTrigger>
        <AccordionContent>
          <div className="mt-2 space-y-4">
            {/* 启用开关 */}
            <div className="flex items-center gap-2">
              <Switch
                checked={providerConfig.enabled}
                onCheckedChange={(checked) => updateProviderConfig(provider, { enabled: checked })}
                id={`switch-${provider}`}
              />
              <Label htmlFor={`switch-${provider}`}>启用此提供商</Label>
            </div>

            <p className="text-xs text-muted-foreground">{info.description}</p>

            <Separator />

            {/* API 配置 */}
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>API Key</Label>
                <Input
                  type="password"
                  value={providerConfig.api_key || ''}
                  onChange={(e) => updateProviderConfig(provider, { api_key: e.target.value })}
                  placeholder="输入 API Key"
                />
              </div>
              <div className="space-y-2">
                <Label>Base URL</Label>
                <Input
                  value={providerConfig.base_url || ''}
                  onChange={(e) => updateProviderConfig(provider, { base_url: e.target.value })}
                  placeholder="https://api.example.com/v1"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Organization (可选)</Label>
                <Input
                  value={providerConfig.organization || ''}
                  onChange={(e) => updateProviderConfig(provider, { organization: e.target.value })}
                  placeholder="组织 ID"
                />
              </div>
              <div className="space-y-2">
                <Label>Project (可选)</Label>
                <Input
                  value={providerConfig.project || ''}
                  onChange={(e) => updateProviderConfig(provider, { project: e.target.value })}
                  placeholder="项目 ID"
                />
              </div>
            </div>

            <Separator />

            {/* 模型列表 */}
            <div className="space-y-2">
              <Label>模型列表</Label>
              <div className="flex gap-2">
                <Input
                  value={newModelInputs[provider] || ''}
                  onChange={(e) => setNewModelInputs({ ...newModelInputs, [provider]: e.target.value })}
                  placeholder="输入模型名称，如 gpt-4"
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddModel(provider);
                    }
                  }}
                  className="flex-1"
                />
                <Button variant="outline" onClick={() => handleAddModel(provider)}>
                  添加
                </Button>
                <Button
                  variant="outline"
                  onClick={() => fetchModelsFromProvider(provider)}
                  disabled={fetchingModels[provider]}
                >
                  {fetchingModels[provider] ? (
                    <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                  ) : (
                    <Download className="h-4 w-4 mr-1" />
                  )}
                  {fetchingModels[provider] ? '获取中...' : '获取列表'}
                </Button>
              </div>

              {models.length > 0 ? (
                <div className="flex flex-col gap-1 mt-2 max-h-[200px] overflow-y-auto p-2 border rounded bg-background">
                  {models.map((model) => (
                    <div key={model} className="flex items-center justify-between px-2 py-1.5 bg-muted rounded text-[13px] hover:bg-muted/80">
                      <span>{model}</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 w-6 p-0"
                        onClick={() => handleRemoveModel(provider, model)}
                      >
                        <X className="h-3 w-3" />
                      </Button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">暂无模型，请添加模型名称</p>
              )}
            </div>

            {/* 默认模型 */}
            {models.length > 0 && (
              <div className="space-y-2">
                <Label>默认模型</Label>
                <Select
                  value={providerConfig.default_model || ''}
                  onValueChange={(value) => updateProviderConfig(provider, { default_model: value })}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="选择默认模型" />
                  </SelectTrigger>
                  <SelectContent>
                    {models.map((model) => (
                      <SelectItem key={model} value={model}>{model}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        </AccordionContent>
      </AccordionItem>
    );
  };

  if (loading) {
    return (
      <div className="flex flex-col h-screen bg-muted overflow-y-auto">
        <div className="flex justify-center items-center h-[200px]">
          <Loader2 className="h-6 w-6 animate-spin mr-2" />
          <span className="text-muted-foreground">加载配置中...</span>
        </div>
      </div>
    );
  }

  const enabledProviders = getEnabledProviders();

  return (
    <div className="flex flex-col h-screen bg-muted overflow-y-auto">
      <div className="flex flex-col items-center w-full">
        {/* 头部 */}
        <div className="px-6 py-5 border-b bg-background flex justify-between items-center w-full max-w-[900px]">
          <span className="text-lg font-semibold">设置</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={loadConfig} disabled={loading}>
              <RefreshCw className="h-4 w-4 mr-1" />
              重新加载
            </Button>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? (
                <Loader2 className="h-4 w-4 mr-1 animate-spin" />
              ) : (
                <Save className="h-4 w-4 mr-1" />
              )}
              {saving ? '保存中...' : '保存配置'}
            </Button>
          </div>
        </div>

        {/* 内容区 */}
        <div className="p-6 w-full max-w-[900px]">
          {/* 全局设置 */}
          <div className="mb-6">
            <div className="mb-3 flex items-center gap-2">
              <span className="text-base font-semibold">全局设置</span>
            </div>
            <Card>
              <CardContent className="pt-6">
                <div className="space-y-2 mb-4">
                  <Label>默认提供商</Label>
                  {enabledProviders.length > 0 ? (
                    <Select
                      value={config?.default_provider || ''}
                      onValueChange={(value) => handleDefaultProviderChange(value as ModelProvider)}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="选择默认提供商" />
                      </SelectTrigger>
                      <SelectContent>
                        {enabledProviders.map((p) => (
                          <SelectItem key={p} value={p}>{PROVIDER_INFO[p].label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <p className="text-xs text-muted-foreground">请先在下方启用至少一个提供商</p>
                  )}
                </div>
                <p className="text-xs text-muted-foreground">默认提供商将用于新对话的初始模型选择</p>
              </CardContent>
            </Card>
          </div>

          {/* 提供商配置 */}
          <div className="mb-6">
            <div className="mb-3 flex items-center gap-2">
              <span className="text-base font-semibold">提供商配置</span>
              <Badge variant="secondary" className="text-xs">
                {enabledProviders.length} 个已启用
              </Badge>
            </div>
            <Card>
              <CardContent className="pt-6">
                <Accordion type="multiple">
                  {ALL_PROVIDERS.map((provider) => renderProviderConfig(provider))}
                </Accordion>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}

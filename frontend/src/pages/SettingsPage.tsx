import { useEffect, useState, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { TextTooltip } from '@/components/ui/text-tooltip';
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
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Loader2, Plus, Trash2, Eye, EyeOff, Settings } from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '../api/config';
import { modelApi } from '../api/model';
import type { ConfigData, ModelProviderConfig } from '../types/model';

// 默认提供商配置模板
const DEFAULT_PROVIDER_CONFIG: ModelProviderConfig = {
  name: '',
  models: [],
  api_key: '',
  base_url: '',
  organization: '',
  project: '',
  hidden_models: [],
  enabled: false,
};

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

export default function SettingsPage() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);

  // 添加提供商对话框
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [newProviderName, setNewProviderName] = useState('');
  const [newProviderId, setNewProviderId] = useState('');
  const [newProviderUrl, setNewProviderUrl] = useState('');
  const [newProviderKey, setNewProviderKey] = useState('');
  const [adding, setAdding] = useState(false);

  // 提供商配置对话框
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editProviderId, setEditProviderId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ModelProviderConfig>({ ...DEFAULT_PROVIDER_CONFIG });
  const [editNewModelInput, setEditNewModelInput] = useState('');
  const [editFetchingModels, setEditFetchingModels] = useState(false);
  const [editSaving, setEditSaving] = useState(false);

  // 删除确认
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
    } catch (err) {
      toast.error('加载配置失败: ' + (err instanceof Error ? err.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  const getEnabledProviders = (): string[] => {
    if (!config) return [];
    return Object.keys(config.provider).filter(p => config.provider[p]?.enabled);
  };

  const getProviderDisplayName = (providerId: string): string => {
    return config?.provider?.[providerId]?.name || providerId;
  };

  const getVisibleProviderModels = (providerId: string): string[] => {
    const provider = config?.provider?.[providerId];
    if (!provider) return [];
    const hidden = new Set(provider.hidden_models || []);
    return (provider.models || []).filter((model) => !hidden.has(model));
  };

  const sanitizeProviderConfig = (provider: ModelProviderConfig): ModelProviderConfig => {
    const { default_model: _ignored, ...rest } = provider as ModelProviderConfig & { default_model?: string };
    return rest;
  };

  // ─── 默认提供商即时保存 ───
  const handleDefaultProviderChange = async (provider: string) => {
    if (!config) return;
    const prev = config.default_provider;
    const prevModel = config.default_model || '';
    const visibleModels = getVisibleProviderModels(provider);
    const nextModel = visibleModels.includes(prevModel) ? prevModel : (visibleModels[0] || '');
    setConfig({ ...config, default_provider: provider, default_model: nextModel });
    try {
      await configApi.update({ default_provider: provider, default_model: nextModel });
      toast.success('默认提供商已更新');
    } catch {
      setConfig(c => c ? { ...c, default_provider: prev, default_model: prevModel } : c);
      toast.error('保存失败');
    }
  };

  const handleDefaultModelChange = async (model: string) => {
    if (!config) return;
    const prev = config.default_model || '';
    setConfig({ ...config, default_model: model });
    try {
      await configApi.update({ default_model: model });
      toast.success('默认模型已更新');
    } catch {
      setConfig(c => c ? { ...c, default_model: prev } : c);
      toast.error('保存失败');
    }
  };

  // ─── 添加提供商 ───
  const handleOpenAddDialog = () => {
    setNewProviderName('');
    setNewProviderId('');
    setNewProviderUrl('');
    setNewProviderKey('');
    setAddDialogOpen(true);
  };

  const handleNameChange = (name: string) => {
    setNewProviderName(name);
    if (!newProviderId || newProviderId === slugify(newProviderName)) {
      setNewProviderId(slugify(name));
    }
  };

  const handleAddProvider = async () => {
    const id = newProviderId.trim();
    const name = newProviderName.trim();
    if (!id || !name) { toast.error('请输入提供商名称'); return; }
    if (config?.provider?.[id]) { toast.error(`ID "${id}" 已存在`); return; }
    try {
      setAdding(true);
      await configApi.addProvider({ id, name, base_url: newProviderUrl, api_key: newProviderKey });
      toast.success(`"${name}" 已添加`);
      setAddDialogOpen(false);
      await loadConfig();
    } catch (err) {
      toast.error('添加失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setAdding(false);
    }
  };

  // ─── 打开提供商配置对话框 ───
  const openEditDialog = (providerId: string) => {
    const cfg = config?.provider?.[providerId] ?? { ...DEFAULT_PROVIDER_CONFIG, name: providerId };
    setEditProviderId(providerId);
    setEditForm(sanitizeProviderConfig({ ...DEFAULT_PROVIDER_CONFIG, ...cfg, hidden_models: [...(cfg.hidden_models || [])] }));
    setEditNewModelInput('');
    setEditDialogOpen(true);
  };

  // ─── 获取模型列表（先保存当前对话框值，再请求） ───
  const handleFetchModels = async () => {
    if (!editProviderId) return;
    try {
      setEditFetchingModels(true);
      // 先把对话框中的值保存到后端，确保获取列表用的是最新配置
      await configApi.update({
        provider_configs: { [editProviderId]: sanitizeProviderConfig(editForm) },
      });
      const models = await modelApi.list(editProviderId);
      if (models?.length) {
        // 新获取的模型合并到已有列表，去重
        const merged = [...new Set([...editForm.models, ...models])];
        setEditForm(f => ({ ...f, models: merged }));
        toast.success(`获取到 ${models.length} 个模型`);
      } else {
        toast.error('未获取到模型，请检查配置');
      }
    } catch (err) {
      toast.error('获取失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setEditFetchingModels(false);
    }
  };

  // ─── 添加模型到对话框 ───
  const handleEditAddModel = () => {
    const name = editNewModelInput.trim();
    if (!name) return;
    if (!editForm.models.includes(name)) {
      setEditForm(f => ({ ...f, models: [...f.models, name] }));
    }
    setEditNewModelInput('');
  };

  // ─── 切换模型隐藏状态 ───
  const toggleModelHidden = (modelName: string) => {
    setEditForm(f => {
      const hidden = new Set(f.hidden_models || []);
      if (hidden.has(modelName)) {
        hidden.delete(modelName);
      } else {
        hidden.add(modelName);
      }
      return { ...f, hidden_models: [...hidden] };
    });
  };

  // ─── 保存提供商配置 ───
  const handleSaveProvider = async () => {
    if (!editProviderId) return;
    try {
      setEditSaving(true);
      const providerConfig = sanitizeProviderConfig(editForm);
      const update = { provider_configs: { [editProviderId]: providerConfig } } as {
        provider_configs: Record<string, Partial<ModelProviderConfig>>;
        default_model?: string;
      };
      if (config?.default_provider === editProviderId) {
        const hidden = new Set(providerConfig.hidden_models || []);
        const visibleModels = (providerConfig.models || []).filter((model) => !hidden.has(model));
        if (!visibleModels.includes(config.default_model || '')) {
          update.default_model = visibleModels[0] || '';
        }
      }
      await configApi.update(update);
      toast.success('已保存');
      setEditDialogOpen(false);
      await loadConfig();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setEditSaving(false);
    }
  };

  // ─── 删除提供商 ───
  const handleDeleteProvider = async (providerId: string) => {
    try {
      await configApi.deleteProvider(providerId);
      toast.success('已删除');
      setDeleteConfirmId(null);
      setEditDialogOpen(false);
      await loadConfig();
    } catch (err) {
      toast.error('删除失败: ' + (err instanceof Error ? err.message : ''));
    }
  };

  // ─── Loading ───
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
  const providerIds = config ? Object.keys(config.provider) : [];
  const defaultProviderModels = config?.default_provider ? getVisibleProviderModels(config.default_provider) : [];

  return (
    <div className="flex flex-col h-screen bg-muted overflow-y-auto">
      <div className="flex flex-col items-center w-full">
        {/* 头部 */}
        <div className="px-6 py-5 border-b bg-background flex items-center w-full max-w-[900px]">
          <span className="text-lg font-semibold">设置</span>
        </div>

        {/* 内容区 */}
        <div className="p-6 w-full max-w-[900px] space-y-6">
          {/* 全局设置 */}
          <div>
            <div className="mb-3 flex items-center gap-2">
              <span className="text-base font-semibold">全局设置</span>
            </div>
            <Card>
              <CardContent className="pt-6">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label>默认提供商</Label>
                    {enabledProviders.length > 0 ? (
                      <Select
                        value={config?.default_provider || ''}
                        onValueChange={handleDefaultProviderChange}
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="选择默认提供商" />
                        </SelectTrigger>
                        <SelectContent>
                          {enabledProviders.map((p) => (
                            <SelectItem key={p} value={p}>{getProviderDisplayName(p)}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <p className="text-xs text-muted-foreground">请先在下方启用至少一个提供商</p>
                    )}
                  </div>
                  <div className="space-y-2">
                    <Label>默认模型</Label>
                    {config?.default_provider && defaultProviderModels.length > 0 ? (
                      <Select
                        value={config.default_model || ''}
                        onValueChange={handleDefaultModelChange}
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="选择默认模型" />
                        </SelectTrigger>
                        <SelectContent>
                          {defaultProviderModels.map((model) => (
                            <SelectItem key={model} value={model}>{model}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <p className="text-xs text-muted-foreground">默认提供商暂无可见模型</p>
                    )}
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* 提供商列表 */}
          <div>
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-base font-semibold">提供商</span>
                <Badge variant="secondary" className="text-xs">
                  {enabledProviders.length} / {providerIds.length} 已启用
                </Badge>
              </div>
              <Button variant="outline" size="sm" onClick={handleOpenAddDialog}>
                <Plus className="h-4 w-4 mr-1" />
                添加提供商
              </Button>
            </div>
            <Card>
              <CardContent className="pt-6">
                {providerIds.length > 0 ? (
                  <div className="flex flex-col gap-2">
                    {providerIds.map((pid) => {
                      const pc = config!.provider[pid];
                      return (
                        <div
                          key={pid}
                          className="flex items-center justify-between p-3 rounded-lg border bg-background hover:bg-accent/50 cursor-pointer transition-colors"
                          onClick={() => openEditDialog(pid)}
                        >
                          <div className="flex items-center gap-3">
                            <Settings className="h-4 w-4 text-muted-foreground" />
                            <span className="font-medium">{pc.name || pid}</span>
                            {pc.enabled && (
                              <Badge variant="secondary" className="text-xs" style={{ background: 'rgba(95,185,138,0.15)', color: 'var(--accent-green)' }}>
                                已启用
                              </Badge>
                            )}
                          </div>
                          <span className="text-xs text-muted-foreground">
                            {pc.models?.length || 0} 个模型
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="text-center py-8 text-muted-foreground">
                    <p className="mb-2">暂无已配置的提供商</p>
                    <p className="text-sm">点击"添加提供商"来添加你的第一个提供商</p>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>

      {/* ─── 添加提供商对话框 ─── */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="max-w-[480px]">
          <DialogHeader>
            <DialogTitle>添加提供商</DialogTitle>
            <DialogDescription>配置一个新的 AI 模型提供商</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label>显示名称</Label>
              <Input value={newProviderName} onChange={(e) => handleNameChange(e.target.value)} placeholder="例如：My OpenAI" />
            </div>
            <div className="space-y-2">
              <Label>提供商 ID</Label>
              <Input value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)} placeholder="自动生成，可手动修改" />
              <p className="text-xs text-muted-foreground">唯一标识，仅限英文、数字和连字符</p>
            </div>
            <div className="space-y-2">
              <Label>Base URL (可选)</Label>
              <Input value={newProviderUrl} onChange={(e) => setNewProviderUrl(e.target.value)} placeholder="https://api.example.com/v1" />
            </div>
            <div className="space-y-2">
              <Label>API Key (可选)</Label>
              <Input type="password" value={newProviderKey} onChange={(e) => setNewProviderKey(e.target.value)} placeholder="可稍后填写" />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddDialogOpen(false)}>取消</Button>
            <Button onClick={handleAddProvider} disabled={adding || !newProviderName.trim()}>
              {adding && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
              {adding ? '添加中...' : '确认添加'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ─── 提供商配置对话框 ─── */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-[560px] max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editForm.name || editProviderId}</DialogTitle>
            <DialogDescription>配置提供商参数和模型列表</DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-2">
            {/* 启用开关 */}
            <div className="flex items-center gap-2">
              <Switch
                checked={editForm.enabled}
                onCheckedChange={(checked) => setEditForm(f => ({ ...f, enabled: checked }))}
              />
              <Label>启用此提供商</Label>
            </div>

            <Separator />

            {/* API Key + Base URL */}
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>API Key</Label>
                <Input
                  type="password"
                  value={editForm.api_key || ''}
                  onChange={(e) => setEditForm(f => ({ ...f, api_key: e.target.value }))}
                  placeholder="输入 API Key"
                />
              </div>
              <div className="space-y-2">
                <Label>Base URL</Label>
                <Input
                  value={editForm.base_url || ''}
                  onChange={(e) => setEditForm(f => ({ ...f, base_url: e.target.value }))}
                  placeholder="https://api.example.com/v1"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Organization (可选)</Label>
                <Input
                  value={editForm.organization || ''}
                  onChange={(e) => setEditForm(f => ({ ...f, organization: e.target.value }))}
                  placeholder="组织 ID"
                />
              </div>
              <div className="space-y-2">
                <Label>Project (可选)</Label>
                <Input
                  value={editForm.project || ''}
                  onChange={(e) => setEditForm(f => ({ ...f, project: e.target.value }))}
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
                  value={editNewModelInput}
                  onChange={(e) => setEditNewModelInput(e.target.value)}
                  placeholder="输入模型名称"
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleEditAddModel(); } }}
                  className="flex-1"
                />
                <Button variant="outline" onClick={handleEditAddModel}>添加</Button>
                <Button variant="outline" onClick={handleFetchModels} disabled={editFetchingModels}>
                  {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
                  {editFetchingModels ? '获取中...' : '获取列表'}
                </Button>
              </div>

              {editForm.models.length > 0 ? (
                <div className="flex flex-col gap-1 mt-2 max-h-[200px] overflow-y-auto p-2 border rounded bg-background">
                  {editForm.models.map((model) => {
                    const hidden = editForm.hidden_models?.includes(model);
                    return (
                      <div
                        key={model}
                        className={`flex items-center justify-between px-2 py-1.5 rounded text-[13px] transition-colors ${
                          hidden ? 'bg-muted/50 opacity-60' : 'bg-muted hover:bg-muted/80'
                        }`}
                      >
                        <span className={hidden ? 'line-through text-muted-foreground' : ''}>
                          {model}
                        </span>
                        <TextTooltip content={hidden ? '显示此模型' : '隐藏此模型'}>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 w-6 p-0"
                            onClick={() => toggleModelHidden(model)}
                            aria-label={hidden ? '显示此模型' : '隐藏此模型'}
                          >
                            {hidden ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                          </Button>
                        </TextTooltip>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">暂无模型，请添加或点击"获取列表"</p>
              )}
            </div>

          </div>

          <DialogFooter className="flex justify-between">
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive hover:bg-destructive/10"
              onClick={() => editProviderId && setDeleteConfirmId(editProviderId)}
            >
              <Trash2 className="h-4 w-4 mr-1" />
              删除提供商
            </Button>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setEditDialogOpen(false)}>取消</Button>
              <Button onClick={handleSaveProvider} disabled={editSaving}>
                {editSaving && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                保存
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ─── 删除确认对话框 ─── */}
      <Dialog open={!!deleteConfirmId} onOpenChange={() => setDeleteConfirmId(null)}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>
              确定要删除 "{deleteConfirmId ? getProviderDisplayName(deleteConfirmId) : ''}" 吗？此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmId(null)}>取消</Button>
            <Button variant="destructive" onClick={() => deleteConfirmId && handleDeleteProvider(deleteConfirmId)}>
              确认删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

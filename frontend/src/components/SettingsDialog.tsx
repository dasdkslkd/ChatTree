import { useEffect, useState, useCallback } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
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
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import {
  X, Settings, StickyNote, Plus, Trash2, Eye, EyeOff,
  Loader2, Save, Pencil, Server,
} from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '../api/config';
import { modelApi } from '../api/model';
import { usePromptStore } from '../store/promtStore';
import type { ConfigData, ModelProviderConfig, APIFormat } from '../types/model';
import type { Prompt, PromptResponse } from '../types/prompt';

/* ─── Constants ─── */

type SettingsSection = 'providers' | 'prompts';

const SETTINGS_NAV: { key: SettingsSection; label: string; icon: typeof Settings; group: string }[] = [
  { key: 'providers', label: '供应商', icon: Server, group: '应用' },
  { key: 'prompts', label: '提示词', icon: StickyNote, group: '应用' },
];

const API_FORMAT_OPTIONS: { value: APIFormat; label: string; description: string }[] = [
  { value: 'chat_completions', label: 'Chat Completions', description: 'OpenAI 兼容格式' },
  { value: 'responses', label: 'Responses API', description: 'OpenAI Responses API' },
  { value: 'anthropic', label: 'Anthropic', description: 'Anthropic Messages API' },
  { value: 'gemini', label: 'Gemini', description: 'Google Gemini API' },
];

const DEFAULT_PROVIDER_CONFIG: ModelProviderConfig = {
  name: '',
  models: [],
  api_key: '',
  base_url: '',
  organization: '',
  project: '',
  api_format: 'chat_completions',
  hidden_models: [],
  enabled: false,
  default_model: '',
};

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9一-鿿]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

/* ─── Props ─── */

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultSection?: SettingsSection;
}

/* ─── Component ─── */

export function SettingsDialog({ open, onOpenChange, defaultSection = 'providers' }: SettingsDialogProps) {
  const [section, setSection] = useState<SettingsSection>(defaultSection);

  // Reset section when dialog opens
  useEffect(() => {
    if (open) setSection(defaultSection);
  }, [open, defaultSection]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="p-0 gap-0 overflow-hidden border-0"
        style={{
          width: '90vw',
          maxWidth: '860px',
          height: '80vh',
          maxHeight: '640px',
          background: 'var(--bg-elevated)',
          border: '0.5px solid var(--border)',
          borderRadius: '20px',
          boxShadow: 'var(--shadow-2xl)',
        }}
      >
        <div className="flex h-full overflow-hidden">
          {/* Left nav */}
          <nav
            className="flex-shrink-0 flex flex-col overflow-hidden"
            style={{
              width: '220px',
              borderRight: '0.5px solid var(--border)',
            }}
          >
            {/* Nav header */}
            <div
              className="flex items-center justify-between px-4 py-4 flex-shrink-0"
              style={{ borderBottom: '0.5px solid var(--border)' }}
            >
              <span className="text-lg font-semibold" style={{ color: 'var(--fg-85)' }}>设置</span>
            </div>

            {/* Nav items */}
            <div className="flex-1 overflow-y-auto p-2 custom-scrollbar">
              {(() => {
                const groups: Record<string, typeof SETTINGS_NAV> = {};
                SETTINGS_NAV.forEach(item => {
                  if (!groups[item.group]) groups[item.group] = [];
                  groups[item.group].push(item);
                });
                return Object.entries(groups).map(([group, items]) => (
                  <div key={group}>
                    <div
                      className="px-3 pt-4 pb-1 text-xs uppercase"
                      style={{ color: 'var(--fg-tertiary)', letterSpacing: '0.025em' }}
                    >
                      {group}
                    </div>
                    {items.map(item => {
                      const Icon = item.icon;
                      const isActive = section === item.key;
                      return (
                        <div
                          key={item.key}
                          className={cn(
                            'flex items-center gap-2.5 px-3 py-1.5 rounded-lg cursor-pointer transition-colors text-sm',
                          )}
                          style={{
                            color: isActive ? 'var(--fg-85)' : 'var(--fg-secondary)',
                            background: isActive ? 'var(--bg-button-tertiary-active)' : undefined,
                            fontWeight: isActive ? 500 : 400,
                          }}
                          onClick={() => setSection(item.key)}
                          onMouseEnter={(e) => {
                            if (!isActive) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                          }}
                          onMouseLeave={(e) => {
                            if (!isActive) (e.currentTarget as HTMLElement).style.background = '';
                          }}
                        >
                          <Icon
                            className="w-4 h-4 flex-shrink-0"
                            style={{ color: isActive ? 'var(--icon-accent)' : 'var(--icon-tertiary)' }}
                          />
                          <span>{item.label}</span>
                        </div>
                      );
                    })}
                  </div>
                ));
              })()}
            </div>
          </nav>

          {/* Right body */}
          <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
            {section === 'providers' && <ProvidersSection />}
            {section === 'prompts' && <PromptsSection />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/* ─── Providers Section ─── */

function ProvidersSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);

  // Add provider dialog
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [newProviderName, setNewProviderName] = useState('');
  const [newProviderId, setNewProviderId] = useState('');
  const [newProviderFormat, setNewProviderFormat] = useState<APIFormat>('chat_completions');
  const [newProviderUrl, setNewProviderUrl] = useState('');
  const [newProviderKey, setNewProviderKey] = useState('');
  const [adding, setAdding] = useState(false);

  // Edit provider dialog
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editProviderId, setEditProviderId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ModelProviderConfig>({ ...DEFAULT_PROVIDER_CONFIG });
  const [editNewModelInput, setEditNewModelInput] = useState('');
  const [editFetchingModels, setEditFetchingModels] = useState(false);
  const [editSaving, setEditSaving] = useState(false);

  // Delete confirm
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
    } catch (err) {
      toast.error('加载配置失败');
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

  const getApiFormatLabel = (format: string): string => {
    return API_FORMAT_OPTIONS.find(f => f.value === format)?.label || format;
  };

  const handleDefaultProviderChange = async (provider: string) => {
    if (!config) return;
    const prev = config.default_provider;
    setConfig({ ...config, default_provider: provider });
    try {
      await configApi.update({ default_provider: provider });
      toast.success('默认提供商已更新');
    } catch {
      setConfig(c => c ? { ...c, default_provider: prev } : c);
      toast.error('保存失败');
    }
  };

  const handleOpenAddDialog = () => {
    setNewProviderName('');
    setNewProviderId('');
    setNewProviderFormat('chat_completions');
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
      await configApi.addProvider({ id, name, api_format: newProviderFormat, base_url: newProviderUrl, api_key: newProviderKey });
      toast.success(`"${name}" 已添加`);
      setAddDialogOpen(false);
      await loadConfig();
    } catch (err) {
      toast.error('添加失败');
    } finally {
      setAdding(false);
    }
  };

  const openEditDialog = (providerId: string) => {
    const cfg = config?.provider?.[providerId] ?? { ...DEFAULT_PROVIDER_CONFIG, name: providerId };
    setEditProviderId(providerId);
    setEditForm({ ...cfg, hidden_models: [...(cfg.hidden_models || [])] });
    setEditNewModelInput('');
    setEditDialogOpen(true);
  };

  const handleFetchModels = async () => {
    if (!editProviderId) return;
    try {
      setEditFetchingModels(true);
      await configApi.update({ provider_configs: { [editProviderId]: editForm } });
      const models = await modelApi.list(editProviderId);
      if (models?.length) {
        const merged = [...new Set([...editForm.models, ...models])];
        setEditForm(f => ({ ...f, models: merged }));
        toast.success(`获取到 ${models.length} 个模型`);
      } else {
        toast.error('未获取到模型');
      }
    } catch {
      toast.error('获取失败');
    } finally {
      setEditFetchingModels(false);
    }
  };

  const handleEditAddModel = () => {
    const name = editNewModelInput.trim();
    if (!name) return;
    if (!editForm.models.includes(name)) {
      setEditForm(f => ({ ...f, models: [...f.models, name] }));
    }
    setEditNewModelInput('');
  };

  const toggleModelHidden = (modelName: string) => {
    setEditForm(f => {
      const hidden = new Set(f.hidden_models || []);
      if (hidden.has(modelName)) hidden.delete(modelName);
      else hidden.add(modelName);
      return { ...f, hidden_models: [...hidden] };
    });
  };

  const handleSaveProvider = async () => {
    if (!editProviderId) return;
    try {
      setEditSaving(true);
      await configApi.update({ provider_configs: { [editProviderId]: editForm } });
      toast.success('已保存');
      setEditDialogOpen(false);
      await loadConfig();
    } catch {
      toast.error('保存失败');
    } finally {
      setEditSaving(false);
    }
  };

  const handleDeleteProvider = async (providerId: string) => {
    try {
      await configApi.deleteProvider(providerId);
      toast.success('已删除');
      setDeleteConfirmId(null);
      setEditDialogOpen(false);
      await loadConfig();
    } catch {
      toast.error('删除失败');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载中...</span>
      </div>
    );
  }

  const enabledProviders = getEnabledProviders();
  const providerIds = config ? Object.keys(config.provider) : [];

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      {/* Fixed header */}
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>供应商</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置 API 端点与模型</p>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-6">

      {/* Default provider */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: '0.5px solid var(--border)' }}
      >
        <div
          className="px-4 py-2.5 text-sm font-medium"
          style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
        >
          全局设置
        </div>
        <div className="px-4 py-3 space-y-2">
          <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>默认提供商</Label>
          {enabledProviders.length > 0 ? (
            <Select value={config?.default_provider || ''} onValueChange={handleDefaultProviderChange}>
              <SelectTrigger className="w-[240px]">
                <SelectValue placeholder="选择默认提供商" />
              </SelectTrigger>
              <SelectContent>
                {enabledProviders.map((p) => (
                  <SelectItem key={p} value={p}>{getProviderDisplayName(p)}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>请先启用至少一个提供商</p>
          )}
        </div>
      </div>

      {/* Provider list */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: '0.5px solid var(--border)' }}
      >
        <div
          className="px-4 py-2.5 flex items-center justify-between"
          style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提供商</span>
            <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
              {enabledProviders.length}/{providerIds.length}
            </span>
          </div>
          <button
            className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-lg cursor-pointer transition-colors"
            style={{ background: 'var(--bg-button-secondary, rgba(255,247,240,0.06))', color: 'var(--fg-secondary)', border: 'none' }}
            onClick={handleOpenAddDialog}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-secondary-hover, rgba(255,247,240,0.10))'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-secondary, rgba(255,247,240,0.06))'; }}
          >
            <Plus className="h-3.5 w-3.5" />
            添加
          </button>
        </div>
        <div className="divide-y" style={{ '--tw-divide-opacity': 1, '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
          {providerIds.length > 0 ? providerIds.map((pid) => {
            const pc = config!.provider[pid];
            return (
              <div
                key={pid}
                className="flex items-center justify-between px-4 py-2.5 cursor-pointer transition-colors"
                onClick={() => openEditDialog(pid)}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
              >
                <div className="flex items-center gap-3">
                  <Settings className="h-4 w-4" style={{ color: 'var(--icon-tertiary)' }} />
                  <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{pc.name || pid}</span>
                  <span className="text-xs px-1.5 py-0.5 rounded" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                    {getApiFormatLabel(pc.api_format)}
                  </span>
                  {pc.enabled && (
                    <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'rgba(95,185,138,0.15)', color: 'var(--accent-green)' }}>
                      已启用
                    </span>
                  )}
                </div>
                <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  {pc.models?.length || 0} 个模型
                </span>
              </div>
            );
          }) : (
            <div className="px-4 py-8 text-center" style={{ color: 'var(--fg-tertiary)' }}>
              <p className="text-sm">暂无已配置的提供商</p>
            </div>
          )}
        </div>
      </div>
      </div>

      {/* Footer spacer */}
      <div className="flex-shrink-0 h-2" />

      {/* ── Add Provider Dialog ── */}
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
              <Input value={newProviderId} onChange={(e) => setNewProviderId(e.target.value)} placeholder="自动生成" />
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>唯一标识，仅限英文、数字和连字符</p>
            </div>
            <div className="space-y-2">
              <Label>API 格式</Label>
              <Select value={newProviderFormat} onValueChange={(v) => setNewProviderFormat(v as APIFormat)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {API_FORMAT_OPTIONS.map(opt => (
                    <SelectItem key={opt.value} value={opt.value} textValue={opt.label}>
                      <div className="flex flex-col">
                        <span>{opt.label}</span>
                        <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{opt.description}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
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

      {/* ── Edit Provider Dialog ── */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-[780px] max-h-[85vh] overflow-y-auto custom-scrollbar">
          <DialogHeader>
            <DialogTitle>{editForm.name || editProviderId}</DialogTitle>
            <DialogDescription>配置提供商参数和模型列表</DialogDescription>
          </DialogHeader>
          <div className="grid grid-cols-2 gap-10 py-2">
            {/* ── 左列：基本配置 ── */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <Switch checked={editForm.enabled} onCheckedChange={(checked) => setEditForm(f => ({ ...f, enabled: checked }))} />
                <Label>启用此提供商</Label>
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />
              <div className="space-y-2">
                <Label>API 格式</Label>
                <Select value={editForm.api_format || 'chat_completions'} onValueChange={(v) => setEditForm(f => ({ ...f, api_format: v as APIFormat }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {API_FORMAT_OPTIONS.map(opt => (
                      <SelectItem key={opt.value} value={opt.value} textValue={opt.label}>
                        <div className="flex flex-col">
                          <span>{opt.label}</span>
                          <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{opt.description}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="h-px" style={{ background: 'var(--border)' }} />
              <div className="space-y-2">
                <Label>API Key</Label>
                <Input type="password" value={editForm.api_key || ''} onChange={(e) => setEditForm(f => ({ ...f, api_key: e.target.value }))} placeholder="输入 API Key" />
              </div>
              <div className="space-y-2">
                <Label>Base URL</Label>
                <Input value={editForm.base_url || ''} onChange={(e) => setEditForm(f => ({ ...f, base_url: e.target.value }))} placeholder="https://api.example.com/v1" />
              </div>
              <div className="space-y-2">
                <Label>Organization (可选)</Label>
                <Input value={editForm.organization || ''} onChange={(e) => setEditForm(f => ({ ...f, organization: e.target.value }))} placeholder="组织 ID" />
              </div>
              <div className="space-y-2">
                <Label>Project (可选)</Label>
                <Input value={editForm.project || ''} onChange={(e) => setEditForm(f => ({ ...f, project: e.target.value }))} placeholder="项目 ID" />
              </div>
            </div>

            {/* ── 右列：模型管理 ── */}
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>模型列表</Label>
                <div className="flex gap-2">
                  <Input value={editNewModelInput} onChange={(e) => setEditNewModelInput(e.target.value)} placeholder="输入模型名称" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); handleEditAddModel(); } }} className="flex-1" />
                  <Button variant="outline" onClick={handleEditAddModel}>添加</Button>
                </div>
                <Button variant="outline" size="sm" onClick={handleFetchModels} disabled={editFetchingModels} className="w-full">
                  {editFetchingModels ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
                  {editFetchingModels ? '获取中...' : '从 API 获取模型列表'}
                </Button>
                {editForm.models.length > 0 ? (
                  <div className="flex flex-col gap-1 mt-2 max-h-[240px] overflow-y-auto p-2 rounded-lg custom-scrollbar" style={{ border: '0.5px solid var(--border)' }}>
                    {editForm.models.map((model) => {
                      const hidden = editForm.hidden_models?.includes(model);
                      return (
                        <div
                          key={model}
                          className="flex items-center justify-between px-2 py-1.5 rounded text-[13px] transition-colors"
                          style={{ background: hidden ? 'var(--muted)' : 'transparent', opacity: hidden ? 0.6 : 1 }}
                          onMouseEnter={(e) => { if (!hidden) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                          onMouseLeave={(e) => { if (!hidden) (e.currentTarget as HTMLElement).style.background = ''; }}
                        >
                          <span style={hidden ? { textDecoration: 'line-through', color: 'var(--fg-tertiary)' } : { color: 'var(--fg-85)' }}>{model}</span>
                          <button
                            className="w-6 h-6 flex items-center justify-center rounded cursor-pointer bg-transparent border-none"
                            title={hidden ? '显示此模型' : '隐藏此模型'}
                            onClick={() => toggleModelHidden(model)}
                            style={{ color: 'var(--icon-tertiary)' }}
                          >
                            {hidden ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                          </button>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>暂无模型，请添加或点击"获取列表"</p>
                )}
              </div>
              {editForm.models.length > 0 && (
                <div className="space-y-2">
                  <Label>默认模型</Label>
                  <Select value={editForm.default_model || ''} onValueChange={(v) => setEditForm(f => ({ ...f, default_model: v }))}>
                    <SelectTrigger><SelectValue placeholder="选择默认模型" /></SelectTrigger>
                    <SelectContent>
                      {editForm.models.map((m) => (
                        <SelectItem key={m} value={m}>{m}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>
          </div>
          <DialogFooter className="flex justify-between">
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              style={{ color: 'var(--accent-red)' }}
              onClick={() => editProviderId && setDeleteConfirmId(editProviderId)}
            >
              <Trash2 className="h-4 w-4 mr-1" />
              删除
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

      {/* ── Delete Confirm Dialog ── */}
      <Dialog open={!!deleteConfirmId} onOpenChange={() => setDeleteConfirmId(null)}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
            <DialogDescription>
              确定要删除 &ldquo;{deleteConfirmId ? getProviderDisplayName(deleteConfirmId) : ''}&rdquo; 吗？此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteConfirmId(null)}>取消</Button>
            <Button variant="destructive" onClick={() => deleteConfirmId && handleDeleteProvider(deleteConfirmId)}>确认删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ─── Prompts Section ─── */

function PromptsSection() {
  const { prompts, currentPrompt, loading, loadPrompts, loadPrompt, savePrompt, deletePrompt, clearCurrentPrompt } = usePromptStore();

  const [editingPrompt, setEditingPrompt] = useState<Prompt | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [promptToDelete, setPromptToDelete] = useState<PromptResponse | null>(null);

  useEffect(() => { loadPrompts(); }, []);

  const handleSelectPrompt = async (prompt: PromptResponse) => {
    setIsNew(false);
    await loadPrompt(prompt.id);
  };

  useEffect(() => {
    if (currentPrompt && !isNew) {
      setEditingPrompt({ ...currentPrompt });
    }
  }, [currentPrompt]);

  const handleCreateNew = () => {
    clearCurrentPrompt();
    setEditingPrompt({ id: `prompt_${Date.now()}`, title: '新提示词', content: '' });
    setIsNew(true);
  };

  const handleSave = async () => {
    if (!editingPrompt || !editingPrompt.title.trim()) return;
    try {
      setSaving(true);
      await savePrompt(editingPrompt);
      toast.success('保存成功');
      setIsNew(false);
    } catch {
      // error handled by store
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!promptToDelete) return;
    try {
      await deletePrompt(promptToDelete.id);
      toast.success('已删除');
      if (editingPrompt?.id === promptToDelete.id) {
        setEditingPrompt(null);
        setIsNew(false);
      }
    } catch {
      // error handled by store
    } finally {
      setDeleteDialogOpen(false);
      setPromptToDelete(null);
    }
  };

  const handleCancel = () => {
    if (currentPrompt) setEditingPrompt({ ...currentPrompt });
    else setEditingPrompt(null);
    setIsNew(false);
  };

  return (
    <div className="flex h-full">
      {/* Left: prompt list */}
      <div
        className="flex flex-col flex-shrink-0 overflow-hidden"
        style={{ width: '220px', borderRight: '0.5px solid var(--border)' }}
      >
        <div
          className="flex items-center justify-between px-3 py-2.5 flex-shrink-0"
          style={{ borderBottom: '0.5px solid var(--border)' }}
        >
          <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提示词</span>
          <button
            className="w-6 h-6 flex items-center justify-center rounded cursor-pointer bg-transparent border-none"
            style={{ color: 'var(--icon-tertiary)' }}
            onClick={handleCreateNew}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 custom-scrollbar">
          {loading && prompts.length === 0 ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
            </div>
          ) : prompts.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              暂无提示词
            </div>
          ) : (
            prompts.map((prompt) => (
              <div
                key={prompt.id}
                className="flex items-center justify-between px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors group"
                style={{
                  background: currentPrompt?.id === prompt.id ? 'var(--bg-button-tertiary-active)' : undefined,
                  color: 'var(--fg-85)',
                }}
                onClick={() => handleSelectPrompt(prompt)}
                onMouseEnter={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                }}
                onMouseLeave={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = '';
                }}
              >
                <span className="flex-1 truncate text-sm">{prompt.title}</span>
                <button
                  className="w-5 h-5 flex items-center justify-center rounded cursor-pointer bg-transparent border-none opacity-0 group-hover:opacity-100 transition-opacity"
                  style={{ color: 'var(--icon-tertiary)' }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setPromptToDelete(prompt);
                    setDeleteDialogOpen(true);
                  }}
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Right: editor */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {/* Fixed header */}
        <div className="flex-shrink-0 px-6 pt-6 pb-4">
          <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>
            {editingPrompt ? (isNew ? '新建提示词' : '编辑提示词') : '提示词'}
          </h1>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            {editingPrompt ? '配置系统提示词' : '管理系统提示词'}
          </p>
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
          {editingPrompt ? (
            <div className="space-y-4 max-w-[500px]">
              <div className="space-y-2">
                <Label>标题</Label>
                <Input
                  value={editingPrompt.title}
                  onChange={(e) => setEditingPrompt({ ...editingPrompt, title: e.target.value })}
                  placeholder="输入提示词标题"
                />
              </div>
              <div className="space-y-2">
                <Label>内容</Label>
                <Textarea
                  value={editingPrompt.content}
                  onChange={(e) => setEditingPrompt({ ...editingPrompt, content: e.target.value })}
                  placeholder="输入系统提示词内容..."
                  className="min-h-[250px] resize-y text-sm leading-relaxed"
                  style={{ fontFamily: 'var(--font-mono)' }}
                />
              </div>
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                系统提示词将作为对话的上下文，指导 AI 的行为和回复风格。
              </p>
              <div className="flex gap-2 justify-end pt-2">
                <Button variant="outline" onClick={handleCancel}>取消</Button>
                <Button onClick={handleSave} disabled={saving || !editingPrompt.title.trim()}>
                  {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
                  {saving ? '保存中...' : '保存'}
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full" style={{ color: 'var(--fg-tertiary)' }}>
              <Pencil className="h-10 w-10 mb-3" />
              <p className="text-sm">选择一个提示词进行编辑</p>
              <p className="text-xs mt-1">或点击 + 创建新的</p>
            </div>
          )}
        </div>

        {/* Footer spacer */}
        <div className="flex-shrink-0 h-2" />
      </div>

      {/* Delete confirm */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            确定要删除 &ldquo;{promptToDelete?.title}&rdquo; 吗？
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>取消</Button>
            <Button variant="destructive" onClick={handleDelete}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

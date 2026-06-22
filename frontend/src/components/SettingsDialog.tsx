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
import {
  Settings, StickyNote, Plus, Trash2, Eye, EyeOff,
  Loader2, Save, Pencil, Server, Wrench, Terminal, Link2, RefreshCw,
  Boxes, Sparkles, Bot, Package, MessageSquare,
} from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '../api/config';
import { modelApi } from '../api/model';
import { useNavigationStore } from '../store/navigationStore';
import { usePromptStore } from '../store/promtStore';
import type {
  BuiltinCodeToolGroup,
  BuiltinToolExposure,
  ConfigData,
  ModelProviderConfig,
  APIFormat,
  McpServerConfig,
  McpTransport,
  ToolsConfig,
  ToolInventoryStatus,
  CapabilityInventory,
  CapabilityPlugin,
  McpServerStatus,
} from '../types/model';
import type { Prompt, PromptResponse } from '../types/prompt';

/* ─── Constants ─── */

type SettingsSection = 'providers' | 'prompts' | 'mcp' | 'capabilities';

const SETTINGS_NAV: { key: SettingsSection; label: string; icon: typeof Settings; group: string }[] = [
  { key: 'providers', label: '供应商', icon: Server, group: '应用' },
  { key: 'mcp', label: 'MCP', icon: Wrench, group: '应用' },
  { key: 'capabilities', label: '能力', icon: Boxes, group: '应用' },
  { key: 'prompts', label: '提示词', icon: StickyNote, group: '应用' },
];

const API_FORMAT_OPTIONS: { value: APIFormat; label: string; description: string }[] = [
  { value: 'chat_completions', label: 'Chat Completions', description: 'OpenAI 兼容格式' },
  { value: 'responses', label: 'Responses API', description: 'OpenAI Responses API' },
  { value: 'anthropic', label: 'Anthropic', description: 'Anthropic Messages API' },
  { value: 'gemini', label: 'Gemini', description: 'Google Gemini API' },
];

const BUILTIN_EXPOSURE_OPTIONS: { value: BuiltinToolExposure; label: string; description: string }[] = [
  { value: 'coding', label: 'Coding', description: '代码读写、搜索、命令和网页工具' },
  { value: 'minimal', label: 'Minimal', description: '仅基础工具和网页工具' },
  { value: 'full', label: 'Full', description: '暴露所有内置工具，包括 write_file' },
];

const BUILTIN_CODE_GROUP_OPTIONS: { value: BuiltinCodeToolGroup; label: string; description: string }[] = [
  { value: 'read', label: '读取', description: 'list_files, read_file' },
  { value: 'search', label: '搜索', description: 'search_files' },
  { value: 'edit', label: '编辑', description: 'edit_file, apply_patch' },
  { value: 'shell', label: '命令', description: 'run_command' },
  { value: 'write', label: '写入', description: 'write_file' },
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

const DEFAULT_MCP_SERVER: McpServerConfig = {
  enabled: true,
  transport: 'streamable_http',
  url: 'http://localhost:3001',
  endpoint: 'http://localhost:3001',
  bearer_token: '',
  headers: {},
  command: '',
  args: [],
  stdio_framing: 'jsonl',
  env: {},
  cwd: '',
  timeout: 30,
  startup_timeout: 30,
  tool_call_timeout: 120,
  heartbeat_enabled: true,
  heartbeat_interval: 30,
  auto_reconnect: true,
  max_reconnect_attempts: 3,
  http_retries: 2,
  http_retry_backoff: 0.5,
  enabled_tools: null,
  disabled_tools: [],
};

const DEFAULT_TOOLS_CONFIG: ToolsConfig = {
  enabled: true,
  max_rounds: 5,
  max_result_length: 8000,
  builtin: {
    enabled: true,
    exposure: 'coding',
    code: {
      enabled: true,
      groups: ['read', 'search', 'edit', 'shell'],
    },
  },
  mcp: {
    enabled: false,
    servers: {},
  },
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

export function SettingsPageView({ defaultSection = 'providers' }: { defaultSection?: SettingsSection }) {
  const [section, setSection] = useState<SettingsSection>(defaultSection);
  const { openChat } = useNavigationStore();

  useEffect(() => {
    setSection(defaultSection);
  }, [defaultSection]);

  return (
    <div className="flex h-full overflow-hidden" style={{ background: 'var(--bg-surface)' }}>
      {/* Left nav */}
      <nav
        className="app-sidebar flex-shrink-0"
        style={{ width: '300px' }}
      >
        <div className="app-sidebar-topbar">
          <span className="text-base font-semibold" style={{ color: 'var(--fg-85)' }}>设置</span>
        </div>

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
                  className="app-sidebar-project-heading"
                  style={{ letterSpacing: '0.025em' }}
                >
                  {group}
                </div>
                <div className="flex flex-col gap-1">
                  {items.map(item => {
                    const Icon = item.icon;
                    const isActive = section === item.key;
                    return (
                      <button
                        type="button"
                        key={item.key}
                        className={cn('app-sidebar-action', isActive && 'is-active')}
                        onClick={() => setSection(item.key)}
                      >
                        <Icon
                          className="w-4 h-4 flex-shrink-0"
                          style={{ color: isActive ? 'var(--icon-accent)' : 'var(--icon-tertiary)' }}
                        />
                        <span>{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ));
          })()}
        </div>
        <div className="app-sidebar-footer">
          <button
            type="button"
            className="app-sidebar-action"
            onClick={openChat}
            title="返回对话"
          >
            <MessageSquare className="h-4 w-4 shrink-0" />
            <span>返回对话</span>
          </button>
        </div>
      </nav>

      {/* Content */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {section === 'providers' && <ProvidersSection />}
        {section === 'mcp' && <McpSection />}
        {section === 'capabilities' && <CapabilitiesSection />}
        {section === 'prompts' && <PromptsSection />}
      </div>
    </div>
  );
}

export function SettingsDialog({ open, onOpenChange, defaultSection = 'providers' }: SettingsDialogProps) {
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
        <SettingsPageView defaultSection={defaultSection} />
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

/* ─── Capabilities Section ─── */

function CapabilitiesSection() {
  const [inventory, setInventory] = useState<CapabilityInventory | null>(null);
  const [mcpStatus, setMcpStatus] = useState<ToolInventoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadCapabilities = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const [nextInventory, nextMcpStatus] = await Promise.all([
        configApi.getCapabilities(),
        configApi.getMcpStatus(),
      ]);
      setInventory(nextInventory);
      setMcpStatus(nextMcpStatus);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载能力信息失败');
      setInventory(null);
      setMcpStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadCapabilities(); }, [loadCapabilities]);

  const reloadCapabilities = useCallback(async () => {
    try {
      setReloading(true);
      setError(null);
      const nextInventory = await configApi.reloadCapabilities();
      const nextMcpStatus = await configApi.getMcpStatus();
      setInventory(nextInventory);
      setMcpStatus(nextMcpStatus);
      toast.success('能力已重载');
    } catch (err) {
      const message = err instanceof Error ? err.message : '重载能力失败';
      toast.error(message);
      if (!inventory) setError(message);
    } finally {
      setReloading(false);
    }
  }, [inventory]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载能力信息中...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
        <div className="flex-shrink-0 px-6 pt-6 pb-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>能力</h1>
              <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>查看当前可用的技能、代理、插件和 MCP Server</p>
            </div>
            <Button variant="outline" size="sm" className="mt-2 mr-10" onClick={reloadCapabilities} disabled={reloading}>
              <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
              重载
            </Button>
          </div>
        </div>
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 pb-6 text-center">
          <Boxes className="h-9 w-9" style={{ color: 'var(--icon-tertiary)' }} />
          <div>
            <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>加载失败</div>
            <div className="mt-1 max-w-[420px] text-xs" style={{ color: 'var(--fg-tertiary)' }}>{error}</div>
          </div>
          <Button variant="outline" size="sm" className="mt-2 mr-10" onClick={reloadCapabilities} disabled={reloading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
            {reloading ? '重载中' : '重载'}
          </Button>
        </div>
      </div>
    );
  }

  const skills = inventory?.skills || [];
  const agents = inventory?.agents || [];
  const plugins = inventory?.plugins || [];
  const mcpServers = mcpStatus?.mcp_servers || [];
  const hasAnyCapability = skills.length + agents.length + plugins.length + mcpServers.length > 0;

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>能力</h1>
            <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>查看当前可用的技能、代理、插件和 MCP Server</p>
          </div>
          <Button variant="outline" size="sm" onClick={reloadCapabilities} disabled={reloading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
            {reloading ? '重载中' : '重载'}
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-4">
        <div className="grid grid-cols-4 gap-3">
          <CapabilityCountCard label="Skills" value={skills.length} icon={Sparkles} />
          <CapabilityCountCard label="Agents" value={agents.length} icon={Bot} />
          <CapabilityCountCard label="Plugins" value={plugins.length} icon={Package} />
          <CapabilityCountCard label="MCP Servers" value={mcpServers.length} icon={Link2} />
        </div>

        {!hasAnyCapability ? (
          <div className="rounded-xl px-4 py-10 text-center" style={{ border: '0.5px solid var(--border)' }}>
            <Boxes className="mx-auto mb-3 h-9 w-9" style={{ color: 'var(--icon-tertiary)' }} />
            <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>暂无能力</div>
            <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>当前项目还没有发现 Skills、Agents、Plugins 或 MCP Servers。</div>
          </div>
        ) : (
          <>
            <CapabilityGroup title="Skills" count={skills.length} emptyText="暂无 Skills">
              {skills.map(skill => (
                <CapabilityItem
                  key={`${skill.source}:${skill.name}:${skill.path || ''}`}
                  icon={Sparkles}
                  name={skill.name}
                  description={skill.description || skill.when_to_use || '未提供描述'}
                  source={skill.source}
                  path={skill.path}
                  pluginId={skill.plugin_id}
                  pluginName={skill.plugin_name}
                  badges={skill.allowed_tools?.slice(0, 3)}
                />
              ))}
            </CapabilityGroup>

            <CapabilityGroup title="Agents" count={agents.length} emptyText="暂无 Agents">
              {agents.map(agent => (
                <CapabilityItem
                  key={`${agent.source}:${agent.name}:${agent.path || ''}`}
                  icon={Bot}
                  name={agent.name}
                  description={agent.description || '未提供描述'}
                  source={agent.source}
                  path={agent.path}
                  pluginId={agent.plugin_id}
                  pluginName={agent.plugin_name}
                  badges={[...(agent.skills || []), ...(agent.tools || [])].slice(0, 3)}
                />
              ))}
            </CapabilityGroup>

            <CapabilityGroup title="Plugins" count={plugins.length} emptyText="暂无 Plugins">
              {plugins.map(plugin => (
                <PluginCapabilityItem key={plugin.plugin_id} plugin={plugin} />
              ))}
            </CapabilityGroup>

            <CapabilityGroup title="MCP Servers" count={mcpServers.length} emptyText="暂无 MCP Servers">
              {mcpServers.map(server => (
                <McpCapabilityItem key={server.name} server={server} />
              ))}
            </CapabilityGroup>
          </>
        )}
      </div>
    </div>
  );
}

function CapabilityCountCard({ label, value, icon: Icon }: { label: string; value: number; icon: typeof Settings }) {
  return (
    <div className="rounded-xl px-3 py-3" style={{ border: '0.5px solid var(--border)' }}>
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>{label}</div>
          <div className="mt-1 text-xl font-semibold" style={{ color: 'var(--fg-85)' }}>{value}</div>
        </div>
        <Icon className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-accent)' }} />
      </div>
    </div>
  );
}

function CapabilityGroup({ title, count, emptyText, children }: {
  title: string;
  count: number;
  emptyText: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ background: 'var(--bg-elevated-secondary, rgba(255,247,240,0.035))', borderBottom: '0.5px solid var(--border)' }}
      >
        <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{title}</span>
        <span className="text-xs px-1.5 py-0.5 rounded-full" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
          {count}
        </span>
      </div>
      <div className="divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
        {count > 0 ? children : (
          <div className="px-4 py-6 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            {emptyText}
          </div>
        )}
      </div>
    </div>
  );
}

function CapabilityItem({
  icon: Icon,
  name,
  description,
  source,
  path,
  pluginId,
  pluginName,
  badges,
}: {
  icon: typeof Settings;
  name: string;
  description?: string;
  source?: string;
  path?: string | null;
  pluginId?: string | null;
  pluginName?: string | null;
  badges?: string[];
}) {
  return (
    <div className="flex gap-3 px-4 py-3">
      <Icon className="mt-0.5 h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-tertiary)' }} />
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{name}</span>
          {source && <SourceBadge source={source} />}
          {pluginName || pluginId ? <PluginBadge pluginName={pluginName} pluginId={pluginId} /> : null}
        </div>
        {description && (
          <div className="line-clamp-2 text-xs leading-relaxed" style={{ color: 'var(--fg-secondary)' }}>
            {description}
          </div>
        )}
        {path && (
          <div className="truncate text-[11px]" title={path} style={{ color: 'var(--fg-tertiary)' }}>
            {path}
          </div>
        )}
        {badges?.length ? (
          <div className="flex flex-wrap gap-1 pt-0.5">
            {badges.map(badge => (
              <span key={badge} className="max-w-[160px] truncate rounded px-1.5 py-0.5 text-[11px]" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                {badge}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function PluginCapabilityItem({ plugin }: { plugin: CapabilityPlugin }) {
  const mcpCount = plugin.mcp_servers ? Object.keys(plugin.mcp_servers).length : 0;
  const detailParts = [
    plugin.version ? `版本 ${plugin.version}` : '',
    `${plugin.skill_roots?.length || 0} skills roots`,
    `${plugin.agent_roots?.length || 0} agents roots`,
    mcpCount ? `${mcpCount} MCP servers` : '',
  ].filter(Boolean);

  return (
    <CapabilityItem
      icon={Package}
      name={plugin.name || plugin.plugin_id}
      description={plugin.description || plugin.error || detailParts.join(' · ') || '未提供描述'}
      source="plugin"
      path={plugin.root}
      pluginId={plugin.plugin_id}
      pluginName={plugin.name}
      badges={detailParts.slice(0, 3)}
    />
  );
}

function McpCapabilityItem({ server }: { server: McpServerStatus }) {
  const statusView = getMcpRuntimeStatusView(server);
  return (
    <div className="flex gap-3 px-4 py-3">
      <Link2 className="mt-0.5 h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-tertiary)' }} />
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{server.name}</span>
          <span className="flex items-center gap-1.5 rounded px-1.5 py-0.5 text-xs" title={statusView.title} style={{ border: '0.5px solid var(--border)', color: statusView.color }}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: statusView.color }} />
            {statusView.label}
          </span>
          <SourceBadge source={server.source || 'user'} />
          {server.plugin_name || server.plugin_id ? <PluginBadge pluginName={server.plugin_name} pluginId={server.plugin_id} /> : null}
        </div>
        <div className="flex flex-wrap gap-1 pt-0.5">
          {server.transport && (
            <span className="rounded px-1.5 py-0.5 text-[11px]" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
              {server.transport === 'stdio' ? 'stdio' : 'HTTP'}
            </span>
          )}
          <span className="rounded px-1.5 py-0.5 text-[11px]" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
            工具 {server.tools_count ?? 0}
          </span>
        </div>
        {server.error && (
          <div className="truncate text-[11px]" title={server.error} style={{ color: 'var(--destructive, #ef4444)' }}>
            {server.error}
          </div>
        )}
      </div>
    </div>
  );
}

function SourceBadge({ source }: { source: string }) {
  return (
    <span className="rounded px-1.5 py-0.5 text-xs" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
      {sourceLabel(source)}
    </span>
  );
}

function PluginBadge({ pluginName, pluginId }: { pluginName?: string | null; pluginId?: string | null }) {
  const text = pluginName && pluginId ? `${pluginName} / ${pluginId}` : pluginName || pluginId || '';
  return (
    <span className="max-w-[220px] truncate rounded px-1.5 py-0.5 text-xs" title={text} style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}>
      {text}
    </span>
  );
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'system': return '系统';
    case 'user': return '用户';
    case 'project': return '项目';
    case 'plugin': return '插件';
    default: return source;
  }
}

function getMcpRuntimeStatusView(server: McpServerStatus) {
  if (server.enabled === false) {
    return { label: '已禁用', color: 'var(--fg-tertiary)', title: '此 Server 已禁用' };
  }
  if (server.connected) {
    return { label: '已连接', color: 'var(--accent-green)', title: `已连接，${server.tools_count ?? 0} 个工具` };
  }
  if (server.error) {
    return { label: '连接失败', color: 'var(--destructive, #ef4444)', title: server.error };
  }
  return { label: '未连接', color: 'var(--fg-tertiary)', title: '尚未建立运行时连接' };
}

/* ─── MCP Section ─── */

function normalizeToolsConfig(raw?: ToolsConfig): ToolsConfig {
  return {
    ...DEFAULT_TOOLS_CONFIG,
    ...(raw || {}),
    builtin: {
      ...(DEFAULT_TOOLS_CONFIG.builtin || {}),
      ...(raw?.builtin || {}),
      code: {
        ...(DEFAULT_TOOLS_CONFIG.builtin?.code || {}),
        ...(raw?.builtin?.code || {}),
      },
    },
    mcp: {
      ...(DEFAULT_TOOLS_CONFIG.mcp || {}),
      ...(raw?.mcp || {}),
      servers: { ...(raw?.mcp?.servers || {}) },
    },
  };
}

function parseNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function listToText(value?: string[] | null): string {
  return Array.isArray(value) ? value.join('\n') : '';
}

function textToList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map(item => item.trim())
    .filter(Boolean);
}

function textToArgs(value: string): string[] {
  const matches = value.match(/"[^"]*"|'[^']*'|\S+/g) || [];
  return matches.map(item => item.replace(/^(['"])(.*)\1$/, '$2')).filter(Boolean);
}

function recordToText(value?: Record<string, string>): string {
  return Object.entries(value || {}).map(([key, val]) => `${key}=${val}`).join('\n');
}

function textToRecord(value: string): Record<string, string> {
  const record: Record<string, string> = {};
  value.split('\n').forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const separator = trimmed.includes('=') ? '=' : ':';
    const index = trimmed.indexOf(separator);
    if (index <= 0) return;
    const key = trimmed.slice(0, index).trim();
    const val = trimmed.slice(index + 1).trim();
    if (key) record[key] = val;
  });
  return record;
}

function commandToString(value?: string | string[]): string {
  if (Array.isArray(value)) return value[0] || '';
  return value || '';
}

function argsToList(server: McpServerConfig): string[] {
  if (Array.isArray(server.args)) return server.args;
  if (Array.isArray(server.command)) return server.command.slice(1);
  return [];
}

function McpSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [toolsForm, setToolsForm] = useState<ToolsConfig>(normalizeToolsConfig());
  const [runtimeStatus, setRuntimeStatus] = useState<ToolInventoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [connectingServerId, setConnectingServerId] = useState<string | null>(null);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [newServerId, setNewServerId] = useState('');
  const [newServerTransport, setNewServerTransport] = useState<McpTransport>('streamable_http');
  const [argsDraft, setArgsDraft] = useState('');
  const [envDraft, setEnvDraft] = useState('');
  const [headersDraft, setHeadersDraft] = useState('');

  const loadRuntimeStatus = useCallback(async () => {
    try {
      const status = await configApi.getMcpStatus();
      setRuntimeStatus(status);
    } catch {
      setRuntimeStatus(null);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      const normalized = normalizeToolsConfig(data.tools);
      const serverIds = Object.keys(normalized.mcp?.servers || {});
      setConfig(data);
      setToolsForm(normalized);
      setSelectedServerId(current => current && serverIds.includes(current) ? current : null);
      await loadRuntimeStatus();
    } catch {
      toast.error('加载 MCP 配置失败');
    } finally {
      setLoading(false);
    }
  }, [loadRuntimeStatus]);

  useEffect(() => {
    loadConfig();
    const timer = window.setInterval(loadRuntimeStatus, 5000);
    return () => window.clearInterval(timer);
  }, [loadConfig, loadRuntimeStatus]);

  const servers = toolsForm.mcp?.servers || {};
  const serverIds = Object.keys(servers);
  const selectedServer = selectedServerId ? servers[selectedServerId] : null;
  const runtimeByServer = new Map((runtimeStatus?.mcp_servers || []).map(server => [server.name, server]));

  const loadServerDrafts = (server: McpServerConfig) => {
    setArgsDraft(listToText(argsToList(server)));
    setEnvDraft(recordToText(server.env));
    setHeadersDraft(recordToText(server.headers));
  };

  const updateTools = (updater: (current: ToolsConfig) => ToolsConfig) => {
    setToolsForm(current => normalizeToolsConfig(updater(normalizeToolsConfig(current))));
  };

  const withCommittedDrafts = (base: ToolsConfig): ToolsConfig => {
    if (!selectedServerId) return base;
    const currentServer = base.mcp?.servers?.[selectedServerId];
    if (!currentServer) return base;
    return normalizeToolsConfig({
      ...base,
      mcp: {
        ...(base.mcp || {}),
        servers: {
          ...(base.mcp?.servers || {}),
          [selectedServerId]: {
            ...DEFAULT_MCP_SERVER,
            ...currentServer,
            args: textToArgs(argsDraft),
            env: textToRecord(envDraft),
            headers: textToRecord(headersDraft),
          },
        },
      },
    });
  };

  const commitDraftsToForm = () => {
    setToolsForm(current => withCommittedDrafts(normalizeToolsConfig(current)));
  };

  const setMcpEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      mcp: { ...(current.mcp || {}), enabled },
    }));
  };

  const setBuiltinExposure = (exposure: BuiltinToolExposure) => {
    updateTools(current => ({
      ...current,
      builtin: {
        ...(current.builtin || {}),
        exposure,
      },
    }));
  };

  const setBuiltinCodeEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      builtin: {
        ...(current.builtin || {}),
        code: {
          ...(current.builtin?.code || {}),
          enabled,
        },
      },
    }));
  };

  const setBuiltinCodeGroup = (group: BuiltinCodeToolGroup, enabled: boolean) => {
    updateTools(current => {
      const groups = new Set(current.builtin?.code?.groups || []);
      if (enabled) {
        groups.add(group);
      } else {
        groups.delete(group);
      }
      return {
        ...current,
        builtin: {
          ...(current.builtin || {}),
          code: {
            ...(current.builtin?.code || {}),
            groups: Array.from(groups),
          },
        },
      };
    });
  };

  const setServerField = <K extends keyof McpServerConfig>(key: K, value: McpServerConfig[K]) => {
    if (!selectedServerId) return;
    updateTools(current => ({
      ...current,
      mcp: {
        ...(current.mcp || {}),
        servers: {
          ...(current.mcp?.servers || {}),
          [selectedServerId]: {
            ...DEFAULT_MCP_SERVER,
            ...(current.mcp?.servers || {})[selectedServerId],
            [key]: value,
          },
        },
      },
    }));
  };

  const handleOpenAddDialog = () => {
    setNewServerId('');
    setNewServerTransport('streamable_http');
    setAddDialogOpen(true);
  };

  const handleAddServer = () => {
    const id = newServerId.trim();
    if (!id) { toast.error('请输入 Server ID'); return; }
    if (servers[id]) { toast.error(`Server "${id}" 已存在`); return; }
    const newServer: McpServerConfig = {
      ...DEFAULT_MCP_SERVER,
      transport: newServerTransport,
      url: newServerTransport === 'streamable_http' ? DEFAULT_MCP_SERVER.url : '',
      endpoint: newServerTransport === 'streamable_http' ? DEFAULT_MCP_SERVER.endpoint : '',
      command: newServerTransport === 'stdio' ? 'npx' : '',
      args: newServerTransport === 'stdio' ? ['-y'] : [],
    };
    updateTools(current => ({
      ...current,
      mcp: {
        ...(current.mcp || {}),
        enabled: true,
        servers: {
          ...(current.mcp?.servers || {}),
          [id]: newServer,
        },
      },
    }));
    setSelectedServerId(id);
    loadServerDrafts(newServer);
    setEditDialogOpen(true);
    setAddDialogOpen(false);
  };

  const handleDeleteServer = () => {
    if (!selectedServerId) return;
    updateTools(current => {
      const nextServers = { ...(current.mcp?.servers || {}) };
      delete nextServers[selectedServerId];
      return {
        ...current,
        mcp: {
          ...(current.mcp || {}),
          servers: nextServers,
        },
      };
    });
    const nextId = serverIds.find(id => id !== selectedServerId) || null;
    setSelectedServerId(nextId);
    setEditDialogOpen(false);
  };

  const openServerDialog = (id: string) => {
    const server = servers[id];
    if (server) loadServerDrafts(server);
    setSelectedServerId(id);
    setEditDialogOpen(true);
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const committedTools = withCommittedDrafts(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      await loadRuntimeStatus();
      toast.success('工具配置已保存');
      await loadConfig();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleConnectServer = async (id: string) => {
    try {
      setConnectingServerId(id);
      const committedTools = withCommittedDrafts(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      const status = await configApi.connectMcpServer(id);
      setRuntimeStatus(status);
      const serverStatus = status.mcp_servers.find(server => server.name === id);
      if (serverStatus?.connected) {
        toast.success(`MCP Server "${id}" 已连接`);
      } else {
        toast.error(serverStatus?.error || `MCP Server "${id}" 连接失败`);
      }
    } catch (err) {
      toast.error('连接失败: ' + (err instanceof Error ? err.message : ''));
      await loadRuntimeStatus();
    } finally {
      setConnectingServerId(null);
    }
  };

  const getServerStatusView = (id: string, server: McpServerConfig) => {
    const status = runtimeByServer.get(id);
    if (server.enabled === false) {
      return { label: '已禁用', color: 'var(--fg-tertiary)', title: '此 Server 已禁用' };
    }
    if (status?.connected) {
      return { label: '已连接', color: 'var(--accent-green)', title: `已连接，${status.tools_count ?? 0} 个工具` };
    }
    if (status?.error) {
      return { label: '连接失败', color: 'var(--destructive, #ef4444)', title: status.error };
    }
    return { label: '未连接', color: 'var(--fg-tertiary)', title: '尚未建立运行时连接' };
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>MCP</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置工具服务器</p>
      </div>

      <div className="flex-1 overflow-hidden px-6 pb-6">
        <div className="flex h-full min-h-0 flex-col gap-4">
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="grid grid-cols-4 gap-4 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <Label>工具系统</Label>
                <Switch checked={toolsForm.enabled !== false} onCheckedChange={(checked) => updateTools(current => ({ ...current, enabled: checked }))} />
              </div>
              <div className="flex items-center justify-between gap-3">
                <Label>MCP</Label>
                <Switch checked={toolsForm.mcp?.enabled === true} onCheckedChange={setMcpEnabled} />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">最大轮次</Label>
                <Input type="number" min={1} value={toolsForm.max_rounds ?? 5} onChange={(e) => updateTools(current => ({ ...current, max_rounds: parseNumber(e.target.value, 5) }))} />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">结果上限</Label>
                <Input type="number" min={1000} value={toolsForm.max_result_length ?? 8000} onChange={(e) => updateTools(current => ({ ...current, max_result_length: parseNumber(e.target.value, 8000) }))} />
              </div>
            </div>
          </div>

          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <div>
                <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>内置工具</div>
                <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  可见 {runtimeStatus?.model_visible_tools?.length ?? 0} 个
                  {runtimeStatus?.hidden_local_tools?.length ? `，隐藏 ${runtimeStatus.hidden_local_tools.length} 个` : ''}
                </div>
              </div>
              <Switch
                checked={toolsForm.builtin?.enabled !== false}
                onCheckedChange={(checked) => updateTools(current => ({
                  ...current,
                  builtin: { ...(current.builtin || {}), enabled: checked },
                }))}
              />
            </div>
            <div className="grid grid-cols-[220px_1fr] gap-4 px-4 py-3">
              <div className="space-y-2">
                <Label className="text-xs">暴露模式</Label>
                <Select
                  value={toolsForm.builtin?.exposure || 'coding'}
                  onValueChange={(value) => setBuiltinExposure(value as BuiltinToolExposure)}
                >
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {BUILTIN_EXPOSURE_OPTIONS.map(option => (
                      <SelectItem key={option.value} value={option.value} textValue={option.label}>
                        <div className="flex flex-col">
                          <span>{option.label}</span>
                          <span className="text-xs opacity-70">{option.description}</span>
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label className="text-xs">代码工具分组</Label>
                  <Switch
                    checked={toolsForm.builtin?.code?.enabled !== false}
                    onCheckedChange={setBuiltinCodeEnabled}
                  />
                </div>
                <div className="grid grid-cols-5 gap-2">
                  {BUILTIN_CODE_GROUP_OPTIONS.map(option => {
                    const enabledGroups = toolsForm.builtin?.code?.groups || [];
                    const enabled = enabledGroups.includes(option.value);
                    return (
                      <button
                        key={option.value}
                        type="button"
                        className="rounded-lg px-2 py-2 text-left text-xs transition-colors"
                        title={option.description}
                        style={{
                          border: '0.5px solid var(--border)',
                          color: enabled ? 'var(--fg-85)' : 'var(--fg-tertiary)',
                          background: enabled ? 'var(--bg-button-secondary)' : 'transparent',
                        }}
                        onClick={() => setBuiltinCodeGroup(option.value, !enabled)}
                      >
                        <div className="font-medium">{option.label}</div>
                        <div className="truncate opacity-70">{option.description}</div>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>Servers</span>
              <button
                className="flex h-7 items-center gap-1 rounded-lg border-0 px-2 text-xs"
                style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}
                onClick={handleOpenAddDialog}
              >
                <Plus className="h-3.5 w-3.5" />
                添加
              </button>
            </div>

            <div className="flex-1 overflow-y-auto custom-scrollbar divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
              {serverIds.length > 0 ? serverIds.map((id) => {
                const server = servers[id];
                const Icon = server.transport === 'stdio' ? Terminal : Link2;
                const statusView = getServerStatusView(id, server);
                const isConnecting = connectingServerId === id;
                return (
                  <div
                    key={id}
                    className="flex w-full items-center gap-3 border-0 bg-transparent px-4 py-3 text-left transition-colors"
                    style={{ color: 'var(--fg-secondary)' }}
                    onClick={() => openServerDialog(id)}
                    onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'transparent'; }}
                  >
                    <Icon className="h-4 w-4 flex-shrink-0" style={{ color: 'var(--icon-tertiary)' }} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{id}</div>
                      <div className="truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {server.transport === 'stdio'
                          ? [commandToString(server.command), ...argsToList(server)].filter(Boolean).join(' ')
                          : (server.url || server.endpoint || '')}
                      </div>
                    </div>
                    <span className="text-xs px-1.5 py-0.5 rounded" style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}>
                      {server.transport === 'stdio' ? 'stdio' : 'HTTP'}
                    </span>
                    <span className="flex items-center gap-1.5 text-xs" title={statusView.title} style={{ color: 'var(--fg-tertiary)' }}>
                      <span className="h-2 w-2 rounded-full" style={{ background: statusView.color }} />
                      {statusView.label}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || server.enabled === false}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleConnectServer(id);
                      }}
                    >
                      {isConnecting ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
                      {runtimeByServer.get(id)?.connected ? '重连' : '连接'}
                    </Button>
                  </div>
                );
              }) : (
                <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  暂无 MCP Server
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
        <Button variant="outline" onClick={loadConfig} disabled={saving}>重置</Button>
        <Button onClick={handleSave} disabled={saving || !config}>
          {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
          保存
        </Button>
      </div>

      <Dialog
        open={editDialogOpen}
        onOpenChange={(open) => {
          if (!open) commitDraftsToForm();
          setEditDialogOpen(open);
        }}
      >
        <DialogContent className="max-w-[760px] max-h-[85vh] overflow-y-auto custom-scrollbar">
          {selectedServer && selectedServerId ? (
            <>
              <DialogHeader>
                <DialogTitle>{selectedServerId}</DialogTitle>
                <DialogDescription>MCP Server 详细配置</DialogDescription>
              </DialogHeader>
              <div className="space-y-5 py-2">
                {(() => {
                  const statusView = getServerStatusView(selectedServerId, selectedServer);
                  const isConnecting = connectingServerId === selectedServerId;
                  return (
                    <div className="flex items-center justify-between rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--fg-85)' }}>
                          <span className="h-2 w-2 rounded-full" style={{ background: statusView.color }} />
                          {statusView.label}
                        </div>
                        {statusView.title && (
                          <div className="mt-1 truncate text-xs" title={statusView.title} style={{ color: 'var(--fg-tertiary)' }}>
                            {statusView.title}
                          </div>
                        )}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || selectedServer.enabled === false}
                        onClick={() => handleConnectServer(selectedServerId)}
                      >
                        {isConnecting ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                        {runtimeByServer.get(selectedServerId)?.connected ? '重连' : '连接'}
                      </Button>
                    </div>
                  );
                })()}

                <div className="flex items-center justify-between gap-3">
                  <Label>启用此 Server</Label>
                  <div className="flex items-center gap-2">
                    <Switch
                      checked={selectedServer.enabled !== false}
                      onCheckedChange={(checked) => setServerField('enabled', checked)}
                    />
                    <Button variant="ghost" size="sm" className="text-destructive hover:text-destructive" onClick={handleDeleteServer}>
                      <Trash2 className="h-4 w-4 mr-1" />
                      删除
                    </Button>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>传输类型</Label>
                    <Select
                      value={selectedServer.transport}
                      onValueChange={(value) => {
                        const transport = value as McpTransport;
                        setServerField('transport', transport);
                        if (transport === 'streamable_http' && !(selectedServer.url || selectedServer.endpoint)) {
                          setServerField('url', DEFAULT_MCP_SERVER.url);
                          setServerField('endpoint', DEFAULT_MCP_SERVER.endpoint);
                        }
                        if (transport === 'stdio' && !commandToString(selectedServer.command)) {
                          setServerField('command', 'npx');
                          setServerField('args', ['-y']);
                          setArgsDraft('-y');
                        }
                      }}
                    >
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="streamable_http" textValue="HTTP">HTTP</SelectItem>
                        <SelectItem value="stdio" textValue="stdio">stdio</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label>工作目录</Label>
                    <Input value={selectedServer.cwd || ''} onChange={(e) => setServerField('cwd', e.target.value)} placeholder="可选" />
                  </div>
                </div>

                {selectedServer.transport === 'stdio' ? (
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                      <Label>启动命令</Label>
                      <Input
                        value={commandToString(selectedServer.command)}
                        onChange={(e) => setServerField('command', e.target.value)}
                        placeholder="npx"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>stdio framing</Label>
                      <Select
                        value={selectedServer.stdio_framing || 'jsonl'}
                        onValueChange={(value) => setServerField('stdio_framing', value as McpServerConfig['stdio_framing'])}
                      >
                        <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          <SelectItem value="content_length" textValue="Content-Length">Content-Length</SelectItem>
                          <SelectItem value="jsonl" textValue="JSONL">JSONL</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <Label>启动参数</Label>
                      <Textarea
                        value={argsDraft}
                        onChange={(e) => setArgsDraft(e.target.value)}
                        onBlur={() => setServerField('args', textToArgs(argsDraft))}
                        className="min-h-[92px] text-sm"
                        placeholder={'-y\n@modelcontextprotocol/server-filesystem\nD:\\Workspace'}
                      />
                    </div>
                    <div className="col-span-2 space-y-2">
                      <Label>环境变量</Label>
                      <Textarea
                        value={envDraft}
                        onChange={(e) => setEnvDraft(e.target.value)}
                        onBlur={() => setServerField('env', textToRecord(envDraft))}
                        className="min-h-[86px] text-sm"
                        placeholder={'TOKEN=xxx\nDEBUG=true'}
                      />
                    </div>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label>URL</Label>
                      <Input
                        value={selectedServer.url || selectedServer.endpoint || ''}
                        onChange={(e) => {
                          setServerField('url', e.target.value);
                          setServerField('endpoint', e.target.value);
                        }}
                        placeholder="http://localhost:3001"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Bearer Token</Label>
                      <Input
                        type="password"
                        value={selectedServer.bearer_token || ''}
                        onChange={(e) => setServerField('bearer_token', e.target.value)}
                        placeholder="可选"
                      />
                    </div>
                    <div className="space-y-2">
                      <Label>Header key-value</Label>
                      <Textarea
                        value={headersDraft}
                        onChange={(e) => setHeadersDraft(e.target.value)}
                        onBlur={() => setServerField('headers', textToRecord(headersDraft))}
                        className="min-h-[86px] text-sm"
                        placeholder={'X-Api-Key=xxx\nX-Client=ChatTree'}
                      />
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-3 gap-4">
                  <div className="space-y-2">
                    <Label>请求超时</Label>
                    <Input type="number" min={1} value={selectedServer.timeout ?? 30} onChange={(e) => setServerField('timeout', parseNumber(e.target.value, 30))} />
                  </div>
                  <div className="space-y-2">
                    <Label>启动超时</Label>
                    <Input type="number" min={1} value={selectedServer.startup_timeout ?? 30} onChange={(e) => setServerField('startup_timeout', parseNumber(e.target.value, 30))} />
                  </div>
                  <div className="space-y-2">
                    <Label>工具超时</Label>
                    <Input type="number" min={1} value={selectedServer.tool_call_timeout ?? 120} onChange={(e) => setServerField('tool_call_timeout', parseNumber(e.target.value, 120))} />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="rounded-lg p-3 space-y-3" style={{ border: '0.5px solid var(--border)' }}>
                    <div className="flex items-center justify-between">
                      <Label>心跳连接</Label>
                      <Switch checked={selectedServer.heartbeat_enabled !== false} onCheckedChange={(checked) => setServerField('heartbeat_enabled', checked)} />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">心跳间隔</Label>
                      <Input type="number" min={1} value={selectedServer.heartbeat_interval ?? 30} onChange={(e) => setServerField('heartbeat_interval', parseNumber(e.target.value, 30))} />
                    </div>
                  </div>

                  <div className="rounded-lg p-3 space-y-3" style={{ border: '0.5px solid var(--border)' }}>
                    <div className="flex items-center justify-between">
                      <Label>自动重连</Label>
                      <Switch checked={selectedServer.auto_reconnect !== false} onCheckedChange={(checked) => setServerField('auto_reconnect', checked)} />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">重连次数</Label>
                      <Input type="number" min={1} value={selectedServer.max_reconnect_attempts ?? 3} onChange={(e) => setServerField('max_reconnect_attempts', parseNumber(e.target.value, 3))} />
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>HTTP 重试次数</Label>
                    <Input type="number" min={0} value={selectedServer.http_retries ?? 2} onChange={(e) => setServerField('http_retries', parseNumber(e.target.value, 2))} />
                  </div>
                  <div className="space-y-2">
                    <Label>重试退避秒数</Label>
                    <Input type="number" min={0} step={0.1} value={selectedServer.http_retry_backoff ?? 0.5} onChange={(e) => setServerField('http_retry_backoff', parseNumber(e.target.value, 0.5))} />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label>启用工具</Label>
                    <Textarea
                      value={listToText(selectedServer.enabled_tools)}
                      onChange={(e) => {
                        const list = textToList(e.target.value);
                        setServerField('enabled_tools', list.length > 0 ? list : null);
                      }}
                      className="min-h-[80px] text-sm"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>禁用工具</Label>
                    <Textarea
                      value={listToText(selectedServer.disabled_tools)}
                      onChange={(e) => setServerField('disabled_tools', textToList(e.target.value))}
                      className="min-h-[80px] text-sm"
                    />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button
                  variant="outline"
                  onClick={() => {
                    commitDraftsToForm();
                    setEditDialogOpen(false);
                  }}
                >
                  关闭
                </Button>
              </DialogFooter>
            </>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>添加 MCP Server</DialogTitle>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <Label>Server ID</Label>
              <Input value={newServerId} onChange={(e) => setNewServerId(e.target.value)} placeholder="filesystem" />
            </div>
            <div className="space-y-2">
              <Label>传输类型</Label>
              <Select value={newServerTransport} onValueChange={(value) => setNewServerTransport(value as McpTransport)}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="streamable_http" textValue="HTTP">HTTP</SelectItem>
                  <SelectItem value="stdio" textValue="stdio">stdio</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddDialogOpen(false)}>取消</Button>
            <Button onClick={handleAddServer}>添加</Button>
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

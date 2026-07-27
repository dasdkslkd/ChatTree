import { useEffect, useState, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TextTooltip } from '@/components/ui/text-tooltip';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Loader2, Save, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '@/api/config';
import type {
  BuiltinCodeToolGroup,
  BuiltinToolExposure,
  ConfigData,
  ToolsConfig,
  ToolInventoryStatus,
  BuiltinWebStatus,
} from '@/types/model';
import type { ToolPermissionMode } from '@/types/message';
import { normalizeToolsConfig, parseNumber } from '../constants';

const BUILTIN_EXPOSURE_OPTIONS: { value: BuiltinToolExposure; label: string; description: string }[] = [
  { value: 'coding', label: 'Coding', description: '代码读写、搜索、命令和网页工具' },
  { value: 'minimal', label: 'Minimal', description: '仅基础工具和网页工具' },
  { value: 'full', label: 'Full', description: '暴露完整 canonical 工具面' },
];

const BUILTIN_CODE_GROUP_OPTIONS: { value: BuiltinCodeToolGroup; label: string; description: string }[] = [
  { value: 'read', label: '读取', description: 'glob, read' },
  { value: 'search', label: '搜索', description: 'grep' },
  { value: 'edit', label: '编辑', description: 'edit' },
  { value: 'shell', label: '命令', description: 'shell' },
  { value: 'write', label: '写入', description: 'write' },
];

const TOOL_PERMISSION_MODE_OPTIONS: { value: ToolPermissionMode; label: string; description: string }[] = [
  { value: 'auto_approve', label: '自动批准', description: '除显式删除外自动执行工具' },
  { value: 'modify_only', label: '修改前询问', description: '读取自动执行，修改需确认' },
  { value: 'ask_always', label: '总是询问', description: '每次工具调用都需确认' },
  { value: 'plan', label: '计划模式', description: '仅允许只读规划工具' },
];

export function BuiltinToolsSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [toolsForm, setToolsForm] = useState<ToolsConfig>(normalizeToolsConfig());
  const [runtimeStatus, setRuntimeStatus] = useState<ToolInventoryStatus | null>(null);
  const [webStatus, setWebStatus] = useState<BuiltinWebStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checkingWeb, setCheckingWeb] = useState(false);

  const updateTools = (updater: (current: ToolsConfig) => ToolsConfig) => {
    setToolsForm(current => normalizeToolsConfig(updater(normalizeToolsConfig(current))));
  };

  const setDefaultPermissionMode = (mode: ToolPermissionMode) => {
    updateTools(current => ({ ...current, default_permission_mode: mode }));
  };

  const loadRuntimeStatus = useCallback(async (options: { checkWeb?: boolean } = {}) => {
    try {
      const inventory = await configApi.getMcpStatus();
      setRuntimeStatus(inventory);
    } catch {
      setRuntimeStatus(null);
    }
    if (options.checkWeb === false) return;
    try {
      const web = await configApi.getBuiltinWebStatus();
      setWebStatus(web);
    } catch {
      setWebStatus(null);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
      setToolsForm(normalizeToolsConfig(data.tools));
      void loadRuntimeStatus({ checkWeb: true });
    } catch {
      toast.error('加载内置工具配置失败');
    } finally {
      setLoading(false);
    }
  }, [loadRuntimeStatus]);

  useEffect(() => {
    loadConfig();
    const timer = window.setInterval(() => { void loadRuntimeStatus({ checkWeb: false }); }, 5000);
    return () => window.clearInterval(timer);
  }, [loadConfig, loadRuntimeStatus]);

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
      if (enabled) groups.add(group);
      else groups.delete(group);
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

  const setWebSearchEnabled = (enabled: boolean) => {
    updateTools(current => ({
      ...current,
      web_search: {
        ...(current.web_search || {}),
        enabled,
      },
    }));
  };

  const setShellInitialWait = (value: number) => {
    updateTools(current => ({
      ...current,
      builtin: {
        ...(current.builtin || {}),
        code: {
          ...(current.builtin?.code || {}),
          shell_initial_wait_seconds: value,
        },
      },
    }));
  };

  const setWaitAgentTimeout = (value: number) => {
    updateTools(current => ({
      ...current,
      wait_agent_timeout_seconds: value,
    }));
  };

  const setSearxngField = (key: 'searxng_url' | 'language' | 'engines' | 'max_results' | 'timeout', value: string | number) => {
    updateTools(current => ({
      ...current,
      web_search: {
        ...(current.web_search || {}),
        searxng: {
          ...(current.web_search?.searxng || {}),
          [key]: value,
        },
      },
    }));
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const committedTools = normalizeToolsConfig(toolsForm);
      setToolsForm(committedTools);
      await configApi.update({ tools: committedTools });
      toast.success('内置工具配置已保存');
      await loadConfig();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleCheckWeb = async () => {
    try {
      setCheckingWeb(true);
      const committedTools = normalizeToolsConfig(toolsForm);
      await configApi.update({ tools: committedTools });
      setToolsForm(committedTools);
      const nextStatus = await configApi.getBuiltinWebStatus();
      setWebStatus(nextStatus);
      if (nextStatus.available) toast.success('SearXNG 连接可用');
      else toast.error(nextStatus.error || 'SearXNG 不可用');
    } catch (err) {
      toast.error('检查失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setCheckingWeb(false);
    }
  };

  const webView = getBuiltinWebStatusView(webStatus, toolsForm);
  const searxng = toolsForm.web_search?.searxng || {};

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载内置工具配置中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>内置工具</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置本地内置工具、代码工具分组和联网搜索能力</p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-4">
        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <Label>工具系统</Label>
              <Switch checked={toolsForm.enabled !== false} onCheckedChange={(checked) => updateTools(current => ({ ...current, enabled: checked }))} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">默认审批</Label>
              <Select
                value={toolsForm.default_permission_mode || 'auto_approve'}
                onValueChange={(value) => setDefaultPermissionMode(value as ToolPermissionMode)}
              >
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {TOOL_PERMISSION_MODE_OPTIONS.map(option => (
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
            <div className="space-y-1">
              <Label className="text-xs">最大轮次</Label>
              <Input type="number" min={1} value={toolsForm.max_rounds ?? 5} onChange={(e) => updateTools(current => ({ ...current, max_rounds: parseNumber(e.target.value, 5) }))} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">结果上限</Label>
              <Input type="number" min={1000} value={toolsForm.max_result_length ?? 8000} onChange={(e) => updateTools(current => ({ ...current, max_result_length: parseNumber(e.target.value, 8000) }))} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Shell 初始等待(秒)</Label>
              <TextTooltip content="shell 命令启动后等待该秒数，未结束则自动转后台运行">
                <Input type="number" min={1} value={toolsForm.builtin?.code?.shell_initial_wait_seconds ?? 120} onChange={(e) => setShellInitialWait(parseNumber(e.target.value, 120))} />
              </TextTooltip>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Wait Agent 超时(秒)</Label>
              <TextTooltip content="wait_agent 工具默认等待 subagent 结果的秒数，超时后 subagent 继续在后台运行">
                <Input type="number" min={1} value={toolsForm.wait_agent_timeout_seconds ?? 30} onChange={(e) => setWaitAgentTimeout(parseNumber(e.target.value, 30))} />
              </TextTooltip>
            </div>
          </div>
        </div>

        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
            <div>
              <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>本地工具暴露</div>
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
                    <TextTooltip key={option.value} content={option.description}>
                      <button
                        type="button"
                        className="rounded-lg px-2 py-2 text-left text-xs transition-colors"
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
                    </TextTooltip>
                  );
                })}
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
          <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
            <div>
              <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>联网工具</div>
              <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>web</div>
            </div>
            <div className="flex items-center gap-3">
              <TextTooltip content={webView.title}>
                <span className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  <span className="h-2 w-2 rounded-full" style={{ background: webView.color }} />
                  {webView.label}
                </span>
              </TextTooltip>
              <Switch checked={toolsForm.web_search?.enabled !== false} onCheckedChange={setWebSearchEnabled} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4 px-4 py-3">
            <div className="space-y-2 col-span-2">
              <Label>SearXNG URL</Label>
              <Input
                value={searxng.searxng_url || ''}
                onChange={(e) => setSearxngField('searxng_url', e.target.value)}
                placeholder="http://localhost:8888"
              />
            </div>
            <div className="space-y-2">
              <Label>语言</Label>
              <Input value={searxng.language || 'zh-CN'} onChange={(e) => setSearxngField('language', e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>搜索引擎</Label>
              <Input value={searxng.engines || ''} onChange={(e) => setSearxngField('engines', e.target.value)} placeholder="留空使用 SearXNG 默认" />
            </div>
            <div className="space-y-2">
              <Label>最大结果数</Label>
              <Input type="number" min={1} max={20} value={searxng.max_results ?? 10} onChange={(e) => setSearxngField('max_results', parseNumber(e.target.value, 10))} />
            </div>
            <div className="space-y-2">
              <Label>超时秒数</Label>
              <Input type="number" min={1} value={searxng.timeout ?? 15} onChange={(e) => setSearxngField('timeout', parseNumber(e.target.value, 15))} />
            </div>
            {webStatus?.error && (
              <TextTooltip content={webStatus.error}>
                <div className="col-span-2 truncate text-xs" style={{ color: webStatus.available ? 'var(--fg-tertiary)' : 'var(--destructive, #ef4444)' }}>
                  {webStatus.error}
                </div>
              </TextTooltip>
            )}
          </div>
        </div>
      </div>

      <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
        <Button variant="outline" onClick={loadConfig} disabled={saving || checkingWeb}>重置</Button>
        <Button variant="outline" onClick={handleCheckWeb} disabled={saving || checkingWeb || !config}>
          {checkingWeb ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
          检查联网
        </Button>
        <Button onClick={handleSave} disabled={saving || !config}>
          {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
          保存
        </Button>
      </div>
    </div>
  );
}

function getBuiltinWebStatusView(webStatus: BuiltinWebStatus | null, toolsForm: ToolsConfig) {
  if (toolsForm.enabled === false || toolsForm.builtin?.enabled === false || toolsForm.web_search?.enabled === false) {
    return { label: '已禁用', color: 'var(--fg-tertiary)', title: '联网工具已禁用' };
  }
  if (!webStatus) {
    return { label: '未检查', color: 'var(--fg-tertiary)', title: '尚未获取 SearXNG 状态' };
  }
  if (webStatus.available) {
    return { label: '可用', color: 'var(--accent-green)', title: `${webStatus.searxng_url} 可用` };
  }
  return { label: '不可用', color: 'var(--destructive, #ef4444)', title: webStatus.error || `${webStatus.searxng_url} 不可用` };
}

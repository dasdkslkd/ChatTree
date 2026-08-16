import { useEffect, useState, useCallback } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { TextTooltip } from '@/components/ui/text-tooltip';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Sparkles, Bot, Package, Boxes, Loader2, RefreshCw, Save, Settings } from 'lucide-react';
import { toast } from '@/utils/toast';
import { configApi } from '@/api/config';
import { normalizeToolsConfig } from '../constants';
import type {
  CapabilityInventory,
  CapabilityPlugin,
  ConfigData,
  ToolsConfig,
} from '@/types/model';

const MULTI_AGENT_MODE_OPTIONS: { value: 'none' | 'explicit_request_only' | 'proactive'; label: string; description: string }[] = [
  { value: 'explicit_request_only', label: '显式', description: '显式请求时启用 subagent/workflow 工具' },
  { value: 'proactive', label: '自动', description: '允许模型主动使用 subagent/workflow 工具' },
  { value: 'none', label: '关闭', description: '不向模型提供 subagent/workflow 工具' },
];

export function CapabilitiesSection({ view }: { view: 'skills' | 'agents' | 'plugins' }) {
  const [inventory, setInventory] = useState<CapabilityInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [agentsForm, setAgentsForm] = useState<ToolsConfig>(() => normalizeToolsConfig());
  const [saving, setSaving] = useState(false);
  const meta = {
    skills: {
      title: 'Skill',
      description: '查看当前可用的技能',
      icon: Sparkles,
      empty: '暂无 Skill',
    },
    agents: {
      title: 'Agent',
      description: '配置 subagent/workflow 可见性并查看可用代理',
      icon: Bot,
      empty: '暂无 Agent',
    },
    plugins: {
      title: '插件',
      description: '查看当前可用的插件',
      icon: Package,
      empty: '暂无插件',
    },
  }[view];

  const loadCapabilities = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const nextInventory = await configApi.getCapabilities();
      setInventory(nextInventory);
      if (view === 'agents') {
        const data = await configApi.get();
        setConfig(data);
        setAgentsForm(normalizeToolsConfig(data.tools));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载能力信息失败');
      setInventory(null);
    } finally {
      setLoading(false);
    }
  }, [view]);

  useEffect(() => { loadCapabilities(); }, [loadCapabilities]);

  const reloadCapabilities = useCallback(async () => {
    try {
      setReloading(true);
      setError(null);
      const nextInventory = await configApi.reloadCapabilities();
      setInventory(nextInventory);
      toast.success('能力已重载');
    } catch (err) {
      const message = err instanceof Error ? err.message : '重载能力失败';
      toast.error(message);
      if (!inventory) setError(message);
    } finally {
      setReloading(false);
    }
  }, [inventory]);

  const handleSaveAgents = async () => {
    try {
      setSaving(true);
      const committed = normalizeToolsConfig(agentsForm);
      setAgentsForm(committed);
      await configApi.update({ tools: committed });
      toast.success('subagent/workflow 可见性已保存');
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

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
              <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>{meta.title}</h1>
              <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{meta.description}</p>
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
  const count = view === 'skills' ? skills.length : view === 'agents' ? agents.length : plugins.length;
  const EmptyIcon = meta.icon;

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>{meta.title}</h1>
            <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{meta.description}</p>
          </div>
          <Button variant="outline" size="sm" onClick={reloadCapabilities} disabled={reloading}>
            <RefreshCw className={cn('h-3.5 w-3.5 mr-1', reloading && 'animate-spin')} />
            {reloading ? '重载中' : '重载'}
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-4">
        <div className="grid grid-cols-1 gap-3">
          <CapabilityCountCard label={meta.title} value={count} icon={meta.icon} />
        </div>

        {view === 'agents' && (
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>subagent/workflow 可见性</span>
              <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>新对话默认</span>
            </div>
            <div className="grid grid-cols-1 gap-4 px-4 py-3">
              <div className="space-y-1.5">
                <Label className="text-xs">模式</Label>
                <Select
                  value={agentsForm.default_multi_agent_mode || 'explicit_request_only'}
                  onValueChange={(value) => setAgentsForm(current => ({ ...current, default_multi_agent_mode: value as 'none' | 'explicit_request_only' | 'proactive' }))}
                >
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {MULTI_AGENT_MODE_OPTIONS.map(option => (
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
              <div className="flex justify-end">
                <Button variant="outline" size="sm" onClick={handleSaveAgents} disabled={saving || !config}>
                  {saving ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <Save className="h-3.5 w-3.5 mr-1" />}
                  保存
                </Button>
              </div>
            </div>
          </div>
        )}

        {count === 0 ? (
          <div className="rounded-xl px-4 py-10 text-center" style={{ border: '0.5px solid var(--border)' }}>
            <EmptyIcon className="mx-auto mb-3 h-9 w-9" style={{ color: 'var(--icon-tertiary)' }} />
            <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{meta.empty}</div>
          </div>
        ) : (
          <>
            {view === 'skills' && <CapabilityGroup title="Skill" count={skills.length} emptyText="暂无 Skill">
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
            </CapabilityGroup>}

            {view === 'agents' && <CapabilityGroup title="Agent" count={agents.length} emptyText="暂无 Agent">
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
            </CapabilityGroup>}

            {view === 'plugins' && <CapabilityGroup title="插件" count={plugins.length} emptyText="暂无插件">
              {plugins.map(plugin => (
                <PluginCapabilityItem key={plugin.plugin_id} plugin={plugin} />
              ))}
            </CapabilityGroup>}
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
        style={{ background: 'var(--bg-button-secondary)', borderBottom: '0.5px solid var(--border)' }}
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
          <TextTooltip content={path}>
            <div className="truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
              {path}
            </div>
          </TextTooltip>
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
    <TextTooltip content={text}>
      <span className="max-w-[220px] truncate rounded px-1.5 py-0.5 text-xs" style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}>
        {text}
      </span>
    </TextTooltip>
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

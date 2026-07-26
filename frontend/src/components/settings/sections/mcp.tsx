import { useEffect, useState, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { TextTooltip } from '@/components/ui/text-tooltip';
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
import { Plus, Trash2, Loader2, Save, RefreshCw, Wrench, Link2 } from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '@/api/config';
import type {
  ConfigData,
  McpServerConfig,
  McpTransport,
  ToolsConfig,
  ToolInventoryStatus,
} from '@/types/model';
import { normalizeToolsConfig, parseNumber } from '../constants';

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
  auto_start: undefined,
  auto_reconnect: true,
  max_reconnect_attempts: 3,
  http_retries: 2,
  http_retry_backoff: 0.5,
  enabled_tools: null,
  disabled_tools: [],
};

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

export function McpSection() {
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
      auto_start: newServerTransport === 'stdio' ? false : true,
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

  const handleDisconnectServer = async (id: string) => {
    try {
      setConnectingServerId(id);
      const status = await configApi.disconnectMcpServer(id);
      setRuntimeStatus(status);
      toast.success(`MCP Server "${id}" 已断开`);
    } catch (err) {
      toast.error('断开失败: ' + (err instanceof Error ? err.message : ''));
      await loadRuntimeStatus();
    } finally {
      setConnectingServerId(null);
    }
  };

  const handleToggleServerConnection = async (id: string) => {
    if (runtimeByServer.get(id)?.connected) {
      await handleDisconnectServer(id);
      return;
    }
    await handleConnectServer(id);
  };

  const getServerStatusView = (id: string, server: McpServerConfig) => {
    const status = runtimeByServer.get(id);
    if (server.enabled === false) {
      return { label: '已禁用', color: 'var(--fg-tertiary)', title: '此 Server 已禁用' };
    }
    if (status?.connected) {
      return { label: '运行中', color: 'var(--accent-green)', title: `运行中，${status.tools_count ?? 0} 个工具` };
    }
    if (status?.error) {
      return { label: '连接失败', color: 'var(--destructive, #ef4444)', title: status.error };
    }
    if (server.transport === 'stdio' && status?.auto_start === false) {
      return { label: '未启动', color: 'var(--fg-tertiary)', title: 'stdio MCP 默认不随 Server 启动，可手动连接' };
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
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>配置 MCP Server 与连接状态</p>
      </div>

      <div className="flex-1 overflow-hidden px-6 pb-6">
        <div className="flex h-full min-h-0 flex-col gap-4">
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="grid grid-cols-3 gap-4 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>MCP</div>
                  <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>启用 MCP Server 工具</div>
                </div>
                <Switch checked={toolsForm.mcp?.enabled === true} onCheckedChange={setMcpEnabled} />
              </div>
              <div className="rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>工具系统</div>
                <div className="mt-1 text-sm font-medium" style={{ color: toolsForm.enabled === false ? 'var(--destructive, #ef4444)' : 'var(--fg-85)' }}>
                  {toolsForm.enabled === false ? '已禁用' : '已启用'}
                </div>
              </div>
              <div className="rounded-lg px-3 py-2" style={{ border: '0.5px solid var(--border)' }}>
                <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>连接状态</div>
                <div className="mt-1 text-sm font-medium" style={{ color: 'var(--fg-85)' }}>
                  {(runtimeStatus?.mcp_servers || []).filter(server => server.connected).length} / {serverIds.length} 已连接
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
                const Icon = server.transport === 'stdio' ? Wrench : Link2;
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
                    <TextTooltip content={statusView.title}>
                      <span className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        <span className="h-2 w-2 rounded-full" style={{ background: statusView.color }} />
                        {statusView.label}
                      </span>
                    </TextTooltip>
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || server.enabled === false}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleToggleServerConnection(id);
                      }}
                    >
                      {isConnecting ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
                      {runtimeByServer.get(id)?.connected ? '断开' : '连接'}
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
                          <TextTooltip content={statusView.title}>
                            <div className="mt-1 truncate text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                              {statusView.title}
                            </div>
                          </TextTooltip>
                        )}
                      </div>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isConnecting || toolsForm.enabled === false || toolsForm.mcp?.enabled !== true || selectedServer.enabled === false}
                        onClick={() => handleToggleServerConnection(selectedServerId)}
                      >
                        {isConnecting ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                        {runtimeByServer.get(selectedServerId)?.connected ? '断开' : '连接'}
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
                        if (transport === 'streamable_http') setServerField('auto_start', true);
                        if (transport === 'stdio') setServerField('auto_start', false);
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
                      <Label>随 Server 启动</Label>
                      <Switch
                        checked={selectedServer.auto_start ?? selectedServer.transport !== 'stdio'}
                        onCheckedChange={(checked) => setServerField('auto_start', checked)}
                      />
                    </div>
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

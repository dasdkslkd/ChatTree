import { useEffect, useState, useCallback, useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Loader2, Save, Link2, RefreshCw } from 'lucide-react';
import { toast } from '@/utils/toast';
import { createLauncherApi, type LauncherProfileStatus } from '@/api/launcher';
import { ChatTreeApiError } from '@/api/errors';
import { getProfileContext } from '@/runtime/profileContext';
import { buildFrontendRoute } from '@/runtime/profileRoute';

export function SshHostsSection() {
  const [path, setPath] = useState('');
  const [text, setText] = useState('');
  const [hosts, setHosts] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<Record<string, LauncherProfileStatus>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyHost, setBusyHost] = useState<string | null>(null);

  const launcher = useMemo(
    () => createLauncherApi(getProfileContext(), window.location.href),
    [],
  );

  const refreshStatuses = useCallback(async (hostAliases: string[]) => {
    const entries = await Promise.all(
      hostAliases.map(async (host) => {
        try {
          const response = await launcher.getSshHostStatus(host);
          return [host, response.session] as const;
        } catch {
          return [host, null] as const;
        }
      }),
    );
    setStatuses(() => {
      const next: Record<string, LauncherProfileStatus> = {};
      for (const [host, status] of entries) {
        if (status) next[host] = status;
      }
      return next;
    });
  }, [launcher]);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const snapshot = await launcher.getSshConfig();
      setPath(snapshot.path);
      setText(snapshot.text);
      setHosts(snapshot.hosts);
      setWarnings(snapshot.warnings);
      await refreshStatuses(snapshot.hosts);
    } catch {
      toast.error('加载 SSH 配置失败');
    } finally {
      setLoading(false);
    }
  }, [launcher, refreshStatuses]);

  useEffect(() => { void load(); }, [load]);

  const handleSave = async () => {
    try {
      setSaving(true);
      const snapshot = await launcher.saveSshConfig(text);
      setPath(snapshot.path);
      setText(snapshot.text);
      setHosts(snapshot.hosts);
      setWarnings(snapshot.warnings);
      await refreshStatuses(snapshot.hosts);
      toast.success('SSH config 已保存');
    } catch {
      toast.error('保存 SSH config 失败');
    } finally {
      setSaving(false);
    }
  };

  const connectHost = async (host: string) => {
    try {
      setBusyHost(host);
      if (window.electronAPI) {
        await window.electronAPI.connectSshHost(host);
        await refreshStatuses([host]);
        return;
      }
      let response;
      try {
        response = await launcher.connectSshHost(host);
      } catch (err) {
        // 远程 server 身份变化（如数据目录被重建）：确认后重新绑定
        const observed = err instanceof ChatTreeApiError
          && err.code === 'server_identity_changed'
          && err.details?.observed_server_instance_id;
        if (typeof observed !== 'string' || !window.confirm(
          `远程 server 身份已变化（数据目录可能被重建）。\n重新绑定到新的 server 实例并连接 ${host}？`,
        )) {
          throw err;
        }
        response = await launcher.connectSshHost(host, observed);
      }
      setStatuses((current) => ({ ...current, [host]: response.session }));
      window.location.href = buildFrontendRoute({ profileId: response.profile_id });
    } catch {
      toast.error(`连接 ${host} 失败`);
    } finally {
      setBusyHost(null);
    }
  };

  const disconnectHost = async (host: string) => {
    try {
      setBusyHost(host);
      const response = await launcher.disconnectSshHost(host);
      setStatuses((current) => ({ ...current, [host]: response.session }));
      toast.success(`${host} 已断开`);
    } catch {
      toast.error(`断开 ${host} 失败`);
    } finally {
      setBusyHost(null);
    }
  };

  const openHost = (host: string) => {
    const status = statuses[host];
    if (!status || status.status !== 'ready') return;
    if (window.electronAPI) {
      void window.electronAPI.connectSshHost(host);
      return;
    }
    window.location.href = buildFrontendRoute({ profileId: status.profile_id });
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
        <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>SSH Hosts</h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{path || '~/.ssh/config'}</p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6 space-y-5">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>~/.ssh/config</Label>
            <Button size="sm" onClick={handleSave} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
              保存
            </Button>
          </div>
          <Textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            className="min-h-[220px] resize-y font-mono text-xs leading-relaxed"
            spellCheck={false}
          />
          {warnings.length > 0 && (
            <div className="space-y-1 text-xs" style={{ color: 'color-mix(in srgb, var(--accent-green) 45%, transparent)' }}>
              {warnings.map((warning) => <div key={warning}>{warning}</div>)}
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label className="text-sm" style={{ color: 'var(--fg-85)' }}>Host</Label>
            <Button variant="outline" size="sm" onClick={load}>
              <RefreshCw className="h-4 w-4 mr-1" />
              刷新
            </Button>
          </div>

          {hosts.length === 0 ? (
            <div
              className="px-3 py-8 text-center text-sm rounded-lg"
              style={{ border: '0.5px solid var(--border)', color: 'var(--fg-tertiary)' }}
            >
              暂无可连接 Host
            </div>
          ) : (
            <div className="space-y-2">
              {hosts.map((host) => {
                const status = statuses[host];
                const ready = status?.status === 'ready';
                const busy = busyHost === host || status?.status === 'connecting';
                return (
                  <div
                    key={host}
                    className="flex items-center gap-3 rounded-lg px-3 py-2"
                    style={{ border: '0.5px solid var(--border)' }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{host}</div>
                      <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {status?.status ?? 'disconnected'}
                        {status?.phase ? ` · ${status.phase}` : ''}
                      </div>
                    </div>
                    <Button variant="outline" size="sm" onClick={() => openHost(host)} disabled={!ready}>
                      打开
                    </Button>
                    {ready ? (
                      <Button variant="outline" size="sm" onClick={() => disconnectHost(host)} disabled={busy}>
                        断开
                      </Button>
                    ) : (
                      <Button size="sm" onClick={() => connectHost(host)} disabled={busy}>
                        {busy ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Link2 className="h-4 w-4 mr-1" />}
                        连接
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

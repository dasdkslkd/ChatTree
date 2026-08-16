import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, FileText, Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';

import { configApi } from '@/api/config';
import { memoryApi } from '@/api/memory';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { TextTooltip } from '@/components/ui/text-tooltip';
import { cn } from '@/lib/utils';
import type { MemoryFileView, MemoryViewResponse, ProjectSettingsItem } from '@/types/model';

type MemoryViewMode = 'global' | 'project';

export function MemorySection() {
  const [mode, setMode] = useState<MemoryViewMode>('global');
  const [projects, setProjects] = useState<ProjectSettingsItem[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [view, setView] = useState<MemoryViewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const requestVersion = useRef(0);
  const selectedProject = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    const version = ++requestVersion.current;
    setLoading(true);
    setView(null);
    try {
      const projectData = await configApi.getProjects();
      const available = projectData.projects || [];
      const current = selectedProject.current;
      const nextProjectId = current && available.some(project => project.id === current)
        ? current
        : available[0]?.id || null;
      const nextView = await memoryApi.get(nextProjectId || undefined);
      if (requestVersion.current !== version) return;
      selectedProject.current = nextProjectId;
      setSelectedProjectId(nextProjectId);
      setProjects(available);
      setView(nextView);
    } catch (error) {
      if (requestVersion.current === version) {
        toast.error('加载记忆失败: ' + (error instanceof Error ? error.message : ''));
      }
    } finally {
      if (requestVersion.current === version) setLoading(false);
    }
  }, []);

  const selectProject = async (projectId: string) => {
    selectedProject.current = projectId;
    setSelectedProjectId(projectId);
    setView(current => current ? { ...current, project: null } : current);
    const version = ++requestVersion.current;
    setLoading(true);
    try {
      const nextView = await memoryApi.get(projectId);
      if (requestVersion.current === version && selectedProject.current === projectId) {
        setView(nextView);
      }
    } catch (error) {
      if (requestVersion.current === version) {
        toast.error('加载项目记忆失败: ' + (error instanceof Error ? error.message : ''));
      }
    } finally {
      if (requestVersion.current === version) setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    return () => { requestVersion.current += 1; };
  }, [refresh]);

  const toggleMemory = async (enabled: boolean) => {
    if (!view) return;
    const previous = view.enabled;
    setView(current => current ? { ...current, enabled } : current);
    setSaving(true);
    try {
      await configApi.update({ memory: { enabled } });
    } catch (error) {
      setView(current => current ? { ...current, enabled: previous } : current);
      toast.error('保存记忆设置失败: ' + (error instanceof Error ? error.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const files = mode === 'global'
    ? view ? [view.global.user, view.global.machine] : []
    : view?.project ? [view.project] : [];

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex flex-shrink-0 items-center justify-between gap-4 px-6 py-5" style={{ borderBottom: '0.5px solid var(--border)' }}>
        <div className="min-w-0">
          <h1 className="text-xl font-semibold" style={{ color: 'var(--fg-85)' }}>记忆</h1>
          {!view?.enabled && <p className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>已关闭</p>}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm" style={{ color: 'var(--fg-secondary)' }}>启用记忆</span>
          <Switch
            checked={view?.enabled ?? true}
            disabled={!view || saving}
            onCheckedChange={toggleMemory}
          />
        </div>
      </div>

      <div className="flex flex-shrink-0 flex-wrap items-center justify-between gap-3 px-6 py-3" style={{ borderBottom: '0.5px solid var(--border)' }}>
        <div className="inline-flex rounded-md p-0.5" style={{ background: 'var(--bg-button-secondary)' }}>
          {(['global', 'project'] as const).map(item => (
            <button
              key={item}
              type="button"
              className={cn('h-8 rounded px-3 text-sm transition-colors', mode === item && 'shadow-sm')}
              style={{
                background: mode === item ? 'var(--bg-elevated)' : 'transparent',
                color: mode === item ? 'var(--fg-85)' : 'var(--fg-tertiary)',
              }}
              onClick={() => setMode(item)}
            >
              {item === 'global' ? '全局记忆' : '项目记忆'}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 items-center gap-2">
          {mode === 'project' && (
            <Select value={selectedProjectId || undefined} onValueChange={selectProject} disabled={projects.length === 0}>
              <SelectTrigger className="w-[240px] max-w-[50vw]">
                <SelectValue placeholder="暂无项目" />
              </SelectTrigger>
              <SelectContent>
                {projects.map(project => (
                  <SelectItem key={project.id} value={project.id}>{project.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <TextTooltip content="刷新">
            <Button variant="outline" size="icon" onClick={() => void refresh()} disabled={loading || saving} aria-label="刷新记忆">
              <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} />
            </Button>
          </TextTooltip>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-5 custom-scrollbar">
        {loading ? (
          <div className="flex h-full items-center justify-center" style={{ color: 'var(--fg-tertiary)' }}>
            <Loader2 className="mr-2 h-5 w-5 animate-spin" />
            加载中...
          </div>
        ) : mode === 'project' && projects.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm" style={{ color: 'var(--fg-tertiary)' }}>暂无项目</div>
        ) : (
          <div className="mx-auto max-w-[820px] space-y-4">
            {files.map(file => <MemoryFile key={file.scope} file={file} />)}
          </div>
        )}
      </div>
    </div>
  );
}

function MemoryFile({ file }: { file: MemoryFileView }) {
  const status = !file.exists ? '尚未生成' : file.valid ? '有效' : file.error || '无效';
  const StatusIcon = file.exists && file.valid ? CheckCircle2 : AlertCircle;
  return (
    <section className="overflow-hidden rounded-md" style={{ border: '0.5px solid var(--border)' }}>
      <header className="flex flex-wrap items-start justify-between gap-3 px-4 py-3" style={{ borderBottom: '0.5px solid var(--border)' }}>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 shrink-0" style={{ color: 'var(--icon-accent)' }} />
            <h2 className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{file.name}</h2>
          </div>
          <p className="mt-1 break-all font-mono text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>{file.path}</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1 text-xs" style={{ color: file.valid ? 'var(--fg-tertiary)' : 'var(--destructive, #ef4444)' }}>
          <StatusIcon className="h-3.5 w-3.5" />
          {status}{file.truncated ? ' · 已截断' : ''}
        </span>
      </header>
      <pre
        className="min-h-[160px] max-h-[320px] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-5 custom-scrollbar"
        style={{ background: 'var(--bg-button-secondary)', color: 'var(--fg-secondary)' }}
      >
        {file.content || (file.exists ? '' : '尚未生成')}
      </pre>
    </section>
  );
}

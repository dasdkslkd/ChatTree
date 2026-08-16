import { useEffect, useState, useCallback } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
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
  FolderOpen, Sparkles, Link2, Bot, History, Trash2,
  Loader2, Save, RefreshCw,
  type LucideIcon,
} from 'lucide-react';
import { toast } from '@/utils/toast';
import { configApi } from '@/api/config';
import { DevEnvironmentEditor } from './dev_environment';
import type {
  CapabilityInventory,
  ToolInventoryStatus,
  ProjectCapabilityConfig,
  ProjectSettingsItem,
} from '@/types/model';

export function ProjectsSection() {
  const [projects, setProjects] = useState<ProjectSettingsItem[]>([]);
  const [inventory, setInventory] = useState<CapabilityInventory | null>(null);
  const [mcpStatus, setMcpStatus] = useState<ToolInventoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [draft, setDraft] = useState<ProjectCapabilityConfig | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteProjectDialogOpen, setDeleteProjectDialogOpen] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);

  const loadProjects = useCallback(async () => {
    try {
      setLoading(true);
      const [projectData, capabilityData, statusData] = await Promise.all([
        configApi.getProjects(),
        configApi.getCapabilities(),
        configApi.getMcpStatus().catch(() => null),
      ]);
      setProjects(projectData.projects || []);
      setInventory(capabilityData);
      setMcpStatus(statusData);
      setSelectedPath(current => {
        if (current && projectData.projects.some(project => project.path === current)) return current;
        return projectData.projects[0]?.path || null;
      });
    } catch (err) {
      toast.error('加载项目配置失败: ' + (err instanceof Error ? err.message : ''));
      setProjects([]);
      setInventory(null);
      setMcpStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  const selectedProject = projects.find(project => project.path === selectedPath) || null;
  useEffect(() => {
    if (!selectedProject) {
      setDraft(null);
      return;
    }
    setDraft({
      label: selectedProject.config?.label || selectedProject.label,
      visible: selectedProject.config?.visible !== false,
      enabled_skills: cloneOptionalList(selectedProject.config?.enabled_skills),
      enabled_mcp_servers: cloneOptionalList(selectedProject.config?.enabled_mcp_servers),
      enabled_agents: cloneOptionalList(selectedProject.config?.enabled_agents),
      dev_environment: selectedProject.config?.dev_environment || {},
    });
  }, [selectedProject]);

  const skillNames = (inventory?.skills || []).map(skill => skill.name).sort((a, b) => a.localeCompare(b));
  const agentNames = (inventory?.agents || []).map(agent => agent.name).sort((a, b) => a.localeCompare(b));
  const mcpServerNames = Array.from(new Set((mcpStatus?.mcp_servers || []).map(server => server.name))).sort((a, b) => a.localeCompare(b));

  const updateDraft = (updater: (current: ProjectCapabilityConfig) => ProjectCapabilityConfig) => {
    setDraft(current => updater(current || {}));
  };

  const setAllowlistItem = (
    key: 'enabled_skills' | 'enabled_mcp_servers' | 'enabled_agents',
    allNames: string[],
    name: string,
    enabled: boolean,
  ) => {
    updateDraft(current => {
      const base = current[key] == null ? allNames : current[key] || [];
      const next = new Set(base);
      if (enabled) next.add(name);
      else next.delete(name);
      return { ...current, [key]: allNames.filter(item => next.has(item)) };
    });
  };

  const setAllowlistAll = (
    key: 'enabled_skills' | 'enabled_mcp_servers' | 'enabled_agents',
    allNames: string[],
    enabled: boolean,
  ) => {
    updateDraft(current => ({ ...current, [key]: enabled ? [...allNames] : [] }));
  };

  const handleSaveProject = async () => {
    if (!selectedProject || !draft) return;
    try {
      setSaving(true);
      await configApi.updateProject(selectedProject.path, {
        ...draft,
        label: draft.label || selectedProject.label,
      });
      toast.success('项目配置已保存');
      window.dispatchEvent(new Event('chattree-projects-updated'));
      await loadProjects();
    } catch (err) {
      toast.error('保存失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteHistory = async () => {
    if (!selectedProject) return;
    try {
      setDeleting(true);
      const result = await configApi.deleteProjectHistory(selectedProject.path, false);
      if (result.skipped_active_ids.length > 0) {
        toast.error(`已删除 ${result.deleted_count} 条，跳过 ${result.skipped_active_ids.length} 条运行中的对话`);
      } else {
        toast.success(`已删除 ${result.deleted_count} 条对话历史`);
      }
      setDeleteDialogOpen(false);
      window.dispatchEvent(new Event('chattree-projects-updated'));
      await loadProjects();
    } catch (err) {
      toast.error('删除失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setDeleting(false);
    }
  };

  const handleDeleteProject = async () => {
    if (!selectedProject) return;
    try {
      setDeletingProject(true);
      await configApi.deleteProject(selectedProject.path);
      setDeleteProjectDialogOpen(false);
      toast.success('项目已删除');
      window.dispatchEvent(new Event('chattree-projects-updated'));
      setSelectedPath(null);
      await loadProjects();
    } catch (err) {
      toast.error('删除项目失败: ' + (err instanceof Error ? err.message : ''));
    } finally {
      setDeletingProject(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载项目中...</span>
      </div>
    );
  }

  return (
    <div className="flex h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex w-[200px] flex-shrink-0 flex-col overflow-hidden" style={{ borderRight: '0.5px solid var(--border)' }}>
        <div className="flex-shrink-0 px-4 pt-5 pb-3">
          <h1 className="text-xl font-semibold" style={{ color: 'var(--fg-85)' }}>项目</h1>
          <p className="mt-1 text-xs" style={{ color: 'var(--fg-secondary)' }}>管理主页面显示与项目能力</p>
        </div>
        <div className="flex-1 overflow-y-auto p-2 custom-scrollbar">
          {projects.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>暂无项目</div>
          ) : projects.map(project => (
            <button
              key={project.path}
              type="button"
              className={cn('app-sidebar-action h-auto items-start py-2', selectedPath === project.path && 'is-active')}
              onClick={() => setSelectedPath(project.path)}
            >
              <FolderOpen className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 text-left">
                <span className="block truncate">{project.label}</span>
                <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                  {project.conversation_count} 条 · {project.path}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {!selectedProject || !draft ? (
          <div className="flex flex-1 flex-col items-center justify-center text-sm" style={{ color: 'var(--fg-tertiary)' }}>
            <FolderOpen className="mb-3 h-10 w-10" />
            选择一个项目
          </div>
        ) : (
          <>
            <div className="flex-shrink-0 px-6 pt-6 pb-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h1 className="truncate text-2xl font-semibold" style={{ color: 'var(--fg-85)' }}>{selectedProject.label}</h1>
                  <p className="mt-1 truncate text-sm" style={{ color: 'var(--fg-secondary)' }}>{selectedProject.path}</p>
                </div>
                <Button variant="outline" size="sm" onClick={loadProjects} disabled={saving}>
                  <RefreshCw className="h-3.5 w-3.5 mr-1" />
                  刷新
                </Button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-6 pb-6 custom-scrollbar">
              <div className="max-w-[760px] space-y-4">
                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--border)' }}>
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>主页面显示</div>
                      <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>关闭后不会在主页面项目列表和新对话项目选择中显示</div>
                    </div>
                    <Switch
                      checked={draft.visible !== false}
                      onCheckedChange={(checked) => updateDraft(current => ({ ...current, visible: checked }))}
                    />
                  </div>
                </div>

                <ProjectAllowlistPanel
                  title="Skill"
                  icon={Sparkles}
                  names={skillNames}
                  value={draft.enabled_skills}
                  emptyText="暂无可配置 Skill"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_skills', skillNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_skills', skillNames, enabled)}
                />
                <ProjectAllowlistPanel
                  title="MCP"
                  icon={Link2}
                  names={mcpServerNames}
                  value={draft.enabled_mcp_servers}
                  emptyText="暂无已发现 MCP Server"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_mcp_servers', mcpServerNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_mcp_servers', mcpServerNames, enabled)}
                />
                <ProjectAllowlistPanel
                  title="Agent"
                  icon={Bot}
                  names={agentNames}
                  value={draft.enabled_agents}
                  emptyText="暂无可配置 Agent"
                  onToggle={(name, enabled) => setAllowlistItem('enabled_agents', agentNames, name, enabled)}
                  onSetAll={(enabled) => setAllowlistAll('enabled_agents', agentNames, enabled)}
                />

                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--border)' }}>
                  <div className="mb-3">
                    <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>开发环境</div>
                    <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                      项目级覆盖：修改 python 即切换该项目默认解释器（如 conda 其它环境），未填写的项沿用全局配置
                    </div>
                  </div>
                  <DevEnvironmentEditor
                    value={draft.dev_environment || {}}
                    onChange={(next) => updateDraft(current => ({ ...current, dev_environment: next }))}
                  />
                </div>

                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--destructive, #ef4444)' }}>
                  <div className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-medium" style={{ color: 'var(--destructive, #ef4444)' }}>
                        <History className="h-4 w-4" />
                        对话历史
                      </div>
                      <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        删除此项目下的 {selectedProject.conversation_count} 条对话历史
                      </div>
                    </div>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={selectedProject.conversation_count === 0}
                      onClick={() => setDeleteDialogOpen(true)}
                    >
                      <Trash2 className="h-4 w-4 mr-1" />
                      批量删除
                    </Button>
                  </div>
                </div>

                <div className="rounded-xl p-4" style={{ border: '0.5px solid var(--destructive, #ef4444)' }}>
                  <div className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-medium" style={{ color: 'var(--destructive, #ef4444)' }}>
                        <Trash2 className="h-4 w-4" />
                        删除项目
                      </div>
                      <div className="mt-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        移除该项目及其全部对话历史，此操作不可撤销
                      </div>
                    </div>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setDeleteProjectDialogOpen(true)}
                    >
                      <Trash2 className="h-4 w-4 mr-1" />
                      删除项目
                    </Button>
                  </div>
                </div>
              </div>
            </div>

            <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
              <Button variant="outline" onClick={loadProjects} disabled={saving}>重置</Button>
              <Button onClick={handleSaveProject} disabled={saving}>
                {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
                保存
              </Button>
            </div>
          </>
        )}
      </div>

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>删除项目对话历史</DialogTitle>
            <DialogDescription>
              将删除此项目下的全部对话历史；运行中的任务会先请求停止。此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)} disabled={deleting}>取消</Button>
            <Button variant="destructive" onClick={handleDeleteHistory} disabled={deleting}>
              {deleting ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Trash2 className="h-4 w-4 mr-1" />}
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteProjectDialogOpen} onOpenChange={setDeleteProjectDialogOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>删除项目</DialogTitle>
            <DialogDescription>
              将删除项目「{selectedProject?.label}」及其全部对话历史；运行中的任务会先请求停止。此操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteProjectDialogOpen(false)} disabled={deletingProject}>取消</Button>
            <Button variant="destructive" onClick={handleDeleteProject} disabled={deletingProject}>
              {deletingProject ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Trash2 className="h-4 w-4 mr-1" />}
              删除项目
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ProjectAllowlistPanel({
  title,
  icon: Icon,
  names,
  value,
  emptyText,
  onToggle,
  onSetAll,
}: {
  title: string;
  icon: LucideIcon;
  names: string[];
  value?: string[] | null;
  emptyText: string;
  onToggle: (name: string, enabled: boolean) => void;
  onSetAll: (enabled: boolean) => void;
}) {
  const selected = value == null ? new Set(names) : new Set(value);
  const isolated = value != null;
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
      <div className="flex items-center justify-between gap-3 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
        <div className="flex min-w-0 items-center gap-2">
          <Icon className="h-4 w-4 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{title}</span>
          <span className="rounded-full px-1.5 py-0.5 text-xs" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
            {selected.size}/{names.length}
          </span>
          <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            {isolated ? '项目隔离' : '继承全部'}
          </span>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => onSetAll(true)}>全部</Button>
          <Button variant="outline" size="sm" className="h-7 px-2 text-xs" onClick={() => onSetAll(false)}>清空</Button>
        </div>
      </div>
      <div className="max-h-[220px] overflow-y-auto custom-scrollbar divide-y" style={{ '--tw-divide-color': 'var(--border-light, rgba(255,247,240,0.05))' } as React.CSSProperties}>
        {names.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>{emptyText}</div>
        ) : names.map(name => (
          <div key={name} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <span className="min-w-0 truncate text-sm" style={{ color: 'var(--fg-secondary)' }}>{name}</span>
            <Switch checked={selected.has(name)} onCheckedChange={(checked) => onToggle(name, checked)} />
          </div>
        ))}
      </div>
    </div>
  );
}

function cloneOptionalList(value?: string[] | null): string[] | null {
  if (value == null) return null;
  return [...value];
}

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { TextTooltip } from '@/components/ui/text-tooltip';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Loader2, Save, Plus, Trash2, Terminal, FolderOpen, X } from 'lucide-react';
import { toast } from 'sonner';
import { configApi } from '@/api/config';
import type { ConfigData, DevEnvironmentConfig } from '@/types/model';

const DEV_TOOL_PRESETS = ['python', 'node', 'npm', 'git', 'java', 'go', 'cargo', 'uv'];

interface PathRow {
  name: string;
  path: string;
}

let detectedCache: Promise<Record<string, string | null>> | null = null;

function loadDetected(): Promise<Record<string, string | null>> {
  detectedCache ??= configApi.getDevEnvironmentDetected().catch(() => ({}));
  return detectedCache;
}

function toToolRows(value: DevEnvironmentConfig): PathRow[] {
  const tools = value.tools || {};
  const rows: PathRow[] = DEV_TOOL_PRESETS.map(name => ({ name, path: tools[name] || '' }));
  for (const [name, path] of Object.entries(tools)) {
    if (!DEV_TOOL_PRESETS.includes(name)) rows.push({ name, path });
  }
  return rows;
}

function toEnvRows(value: DevEnvironmentConfig): PathRow[] {
  return Object.entries(value.environments || {}).map(([name, path]) => ({ name, path }));
}

function rowsToRecord(rows: PathRow[]): Record<string, string> {
  const record: Record<string, string> = {};
  for (const row of rows) {
    const name = row.name.trim();
    const path = row.path.trim();
    if (name && path) record[name] = path;
  }
  return record;
}

function pickFilePath(): Promise<string | null> {
  if (!window.electronAPI?.getPathForFile) {
    toast.error('仅桌面版可获取文件路径');
    return Promise.resolve(null);
  }
  const getPathForFile = window.electronAPI.getPathForFile;
  return new Promise(resolve => {
    const input = document.createElement('input');
    input.type = 'file';
    input.className = 'hidden';
    input.onchange = () => {
      const path = input.files?.[0] ? getPathForFile(input.files[0]) : null;
      input.remove();
      resolve(path || null);
    };
    input.oncancel = () => {
      input.remove();
      resolve(null);
    };
    document.body.appendChild(input);
    input.click();
  });
}

export function DevEnvironmentEditor({
  value,
  onChange,
}: {
  value: DevEnvironmentConfig;
  onChange: (next: DevEnvironmentConfig) => void;
}) {
  const [toolRows, setToolRows] = useState<PathRow[]>(() => toToolRows(value));
  const [envRows, setEnvRows] = useState<PathRow[]>(() => toEnvRows(value));
  const [synced, setSynced] = useState(() => JSON.stringify(value));
  const [detected, setDetected] = useState<Record<string, string | null>>({});

  useEffect(() => {
    let active = true;
    loadDetected().then(result => { if (active) setDetected(result); });
    return () => { active = false; };
  }, []);

  // 渲染期状态调整：外部 value 变化（重置/重新加载）时同步本地草稿
  const snapshot = JSON.stringify(value);
  if (snapshot !== synced) {
    setSynced(snapshot);
    setToolRows(toToolRows(value));
    setEnvRows(toEnvRows(value));
  }

  const emit = (next: DevEnvironmentConfig) => {
    setSynced(JSON.stringify(next));
    onChange(next);
  };

  const updateToolRows = (rows: PathRow[]) => {
    setToolRows(rows);
    emit({ ...value, tools: rowsToRecord(rows) });
  };

  const updateEnvRows = (rows: PathRow[]) => {
    setEnvRows(rows);
    const environments = rowsToRecord(rows);
    const defaultEnvironment = value.default_environment && environments[value.default_environment]
      ? value.default_environment
      : Object.keys(environments)[0] || '';
    emit({ ...value, environments, default_environment: defaultEnvironment });
  };

  const pickToolPath = async (index: number) => {
    const path = await pickFilePath();
    if (path) updateToolRows(toolRows.map((item, i) => i === index ? { ...item, path } : item));
  };

  const pickEnvPath = async (index: number) => {
    const path = await pickFilePath();
    if (path) updateEnvRows(envRows.map((item, i) => i === index ? { ...item, path } : item));
  };

  return (
    <div className="space-y-4">
      <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
        <div className="px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
          <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>默认工具路径</div>
          <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            灰色为系统 PATH 检测到的默认值；选择非 PATH 路径时，其目录会注入命令执行的 PATH
          </div>
        </div>
        <div className="space-y-3 px-4 py-3">
          {toolRows.map((row, index) => (
            <div key={index} className="flex items-center gap-2">
              {DEV_TOOL_PRESETS.includes(row.name) ? (
                <Label className="w-24 shrink-0 text-xs">{row.name}</Label>
              ) : (
                <Input
                  className="w-24 shrink-0 text-xs"
                  value={row.name}
                  placeholder="工具名"
                  onChange={(e) => updateToolRows(toolRows.map((item, i) => i === index ? { ...item, name: e.target.value } : item))}
                />
              )}
              <Input
                className="flex-1"
                readOnly
                value={row.path}
                placeholder={detected[row.name] || '系统 PATH 未检测到'}
              />
              <TextTooltip content="选择可执行文件">
                <Button variant="outline" size="sm" className="h-8 w-8 p-0 shrink-0" onClick={() => pickToolPath(index)}>
                  <FolderOpen className="h-3.5 w-3.5" />
                </Button>
              </TextTooltip>
              {row.path && (
                <TextTooltip content="恢复系统 PATH 默认">
                  <Button variant="outline" size="sm" className="h-8 w-8 p-0 shrink-0"
                    onClick={() => updateToolRows(toolRows.map((item, i) => i === index ? { ...item, path: '' } : item))}>
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </TextTooltip>
              )}
              {!DEV_TOOL_PRESETS.includes(row.name) && (
                <TextTooltip content="删除该条目">
                  <Button variant="outline" size="sm" className="h-8 w-8 p-0 shrink-0"
                    onClick={() => updateToolRows(toolRows.filter((_, i) => i !== index))}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TextTooltip>
              )}
            </div>
          ))}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setToolRows([...toolRows, { name: '', path: '' }])}
          >
            <Plus className="h-3.5 w-3.5 mr-1" />
            添加工具
          </Button>
        </div>
      </div>

      <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
        <div className="px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
          <div className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>并列虚拟环境</div>
          <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
            仅多 venv 项目需要：默认环境注入命令 PATH（裸 python 指向它），其余环境以绝对路径注入模型提示词
          </div>
        </div>
        <div className="space-y-3 px-4 py-3">
          {envRows.length === 0 && (
            <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>未配置并列虚拟环境</div>
          )}
          {envRows.length > 0 && (
            <RadioGroup
              value={value.default_environment || ''}
              onValueChange={(name) => emit({ ...value, default_environment: name })}
            >
              {envRows.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <RadioGroupItem value={row.name} id={`dev-env-default-${index}`} />
                  <Input
                    className="w-32 shrink-0 text-xs"
                    value={row.name}
                    placeholder="环境名"
                    onChange={(e) => updateEnvRows(envRows.map((item, i) => i === index ? { ...item, name: e.target.value } : item))}
                  />
                  <Input
                    className="flex-1"
                    readOnly
                    value={row.path}
                    placeholder="python 解释器绝对路径"
                  />
                  <TextTooltip content="选择可执行文件">
                    <Button variant="outline" size="sm" className="h-8 w-8 p-0 shrink-0" onClick={() => pickEnvPath(index)}>
                      <FolderOpen className="h-3.5 w-3.5" />
                    </Button>
                  </TextTooltip>
                  <TextTooltip content="删除该环境">
                    <Button variant="outline" size="sm" className="h-8 w-8 p-0 shrink-0"
                      onClick={() => updateEnvRows(envRows.filter((_, i) => i !== index))}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </TextTooltip>
                </div>
              ))}
            </RadioGroup>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setEnvRows([...envRows, { name: '', path: '' }])}
          >
            <Plus className="h-3.5 w-3.5 mr-1" />
            添加虚拟环境
          </Button>
        </div>
      </div>
    </div>
  );
}

export function DevEnvironmentSection() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [form, setForm] = useState<DevEnvironmentConfig>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const data = await configApi.get();
      setConfig(data);
      setForm(data.dev_environment || {});
    } catch {
      toast.error('加载开发环境配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadConfig();
  }, []);

  const handleSave = async () => {
    try {
      setSaving(true);
      await configApi.update({ dev_environment: form });
      toast.success('开发环境配置已保存');
      await loadConfig();
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
        <span style={{ color: 'var(--fg-tertiary)' }}>加载开发环境配置中...</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--fg-85)' }}>
          <Terminal className="h-5 w-5" />
          开发环境
        </h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
          配置默认解释器与并列虚拟环境，避免模型重复探测环境
        </p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
        <div className="max-w-[760px]">
          <DevEnvironmentEditor value={form} onChange={setForm} />
        </div>
      </div>

      <div className="flex-shrink-0 flex justify-end gap-2 px-6 pb-5">
        <Button variant="outline" onClick={loadConfig} disabled={saving}>重置</Button>
        <Button onClick={handleSave} disabled={saving || !config}>
          {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
          保存
        </Button>
      </div>
    </div>
  );
}

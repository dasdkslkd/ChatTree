import { useCallback, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Loader2, Database, Archive, HardDrive, Play, LogOut } from 'lucide-react';
import { toast } from '@/utils/toast';
import { storageApi, type StorageStats } from '@/api/storage';
import { createLauncherApi } from '@/api/launcher';
import { getProfileContext } from '@/runtime/profileContext';

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{label}</span>
      <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{value}</span>
    </div>
  );
}

export function StorageSection() {
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [compacting, setCompacting] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  const loadStats = useCallback(async () => {
    try {
      setLoading(true);
      const data = await storageApi.stats();
      setStats(data);
    } catch {
      toast.error('加载存储统计失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  const handleCompact = async () => {
    if (!confirmed) {
      setConfirmed(true);
      return;
    }
    try {
      setCompacting(true);
      await storageApi.compact();
      setCompacting(false);
      setConfirmed(false);
      // 压缩完成：后端已自动退出。与应用一起优雅退出，
      // 避免保持在前端页面轮询已停止的后端。
      toast.success('压缩完成');
      if (window.electronAPI?.quitApp) {
        await window.electronAPI.quitApp();
      } else {
        // 浏览器/纯 Web 环境回退：请求 launcher 优雅关闭。
        await createLauncherApi(
          getProfileContext(),
          window.location.href,
        ).shutdown().catch(() => {});
      }
    } catch (err) {
      setCompacting(false);
      setConfirmed(false);
      toast.error('压缩失败: ' + (err instanceof Error ? err.message : ''));
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载存储统计中...</span>
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="flex items-center justify-center h-full">
        <Button variant="outline" onClick={loadStats}>重新加载</Button>
      </div>
    );
  }

  const reclaimablePercent = stats.logical_bytes > 0
    ? Math.min((stats.reclaimable_bytes / stats.logical_bytes) * 100, 100)
    : 0;
  const canCompact = stats.active_runs === 0 && !compacting;

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--fg-85)' }}>
          <HardDrive className="h-5 w-5" />
          存储占用
        </h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
          查看对话数据与磁盘占用，回收删除操作遗留的碎片空间
        </p>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
        <div className="max-w-[760px] space-y-4">
          {/* 数据库占用 */}
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center gap-2 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <Database className="h-4 w-4" style={{ color: 'var(--icon-accent)' }} />
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>对话数据库 (SQLite)</span>
              {stats.recommended && (
                <span className="text-xs ml-auto px-2 py-0.5 rounded-full" style={{ background: 'rgba(255,120,80,0.15)', color: 'var(--accent-orange)' }}>
                  建议压缩
                </span>
              )}
            </div>
            <div className="px-4 py-3">
              <StatRow label="数据库文件" value={formatBytes(stats.db_file_bytes)} />
              <StatRow label="逻辑内容" value={formatBytes(stats.logical_bytes)} />
              <StatRow label="可回收碎片" value={formatBytes(stats.reclaimable_bytes)} />
              <div className="mt-2 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>碎片占比</span>
                  <span className="text-xs" style={{ color: reclaimablePercent >= 30 ? 'var(--accent-red)' : 'var(--fg-tertiary)' }}>
                    {reclaimablePercent.toFixed(1)}%
                  </span>
                </div>
                <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--bg-button-secondary)' }}>
                  <div
                    className="h-full rounded-full transition-all"
                    style={{
                      width: `${reclaimablePercent}%`,
                      background: reclaimablePercent >= 30 ? 'var(--accent-red)' : 'var(--accent-green)',
                    }}
                  />
                </div>
              </div>
            </div>
          </div>

          {/* 其他占用 */}
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center gap-2 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <Archive className="h-4 w-4" style={{ color: 'var(--icon-accent)' }} />
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>Blob 与运行日志</span>
            </div>
            <div className="px-4 py-3">
              <StatRow label="Blob 文件" value={`${formatBytes(stats.blobs_bytes)} (${stats.blobs_count} 个)`} />
              <StatRow label="运行日志 (JSONL)" value={`${formatBytes(stats.conversations_dir_bytes)} (${stats.run_journals_count} 个)`} />
              <StatRow label="数据目录" value={stats.home} />
            </div>
          </div>

          {/* 压缩操作 */}
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="px-4 py-3 space-y-3">
              <div className="flex items-center gap-2">
                <Play className="h-4 w-4" style={{ color: 'var(--icon-accent)' }} />
                <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>压缩并退出</span>
              </div>
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                压缩需要独占数据库，且结束后应用会自动退出以完成回收。请在操作前保存工作，并确认当前没有运行中的任务。
              </p>
              {stats.active_runs > 0 ? (
                <p className="text-xs" style={{ color: 'var(--accent-red)' }}>
                  当前有 {stats.active_runs} 个运行中任务，压缩暂不可用。
                </p>
              ) : null}
              {compacting ? (
                <div className="flex items-center gap-2 text-sm" style={{ color: 'var(--fg-secondary)' }}>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在压缩，应用即将退出...
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <Button
                    variant="destructive"
                    onClick={handleCompact}
                    disabled={!canCompact}
                  >
                    <LogOut className="h-4 w-4 mr-1" />
                    {confirmed ? '确认压缩并退出' : '压缩并退出'}
                  </Button>
                  {confirmed && !compacting && (
                    <Button variant="outline" size="sm" onClick={() => setConfirmed(false)}>取消</Button>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
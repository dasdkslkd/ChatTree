import { useCallback, useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Loader2, Coins, BarChart3 } from 'lucide-react';
import { toast } from 'sonner';
import { usageApi, type ModelUsage, type UsageRange, type UsageStats } from '@/api/usage';

const RANGES: { key: UsageRange; label: string }[] = [
  { key: '1d', label: '1天' },
  { key: '7d', label: '7天' },
  { key: '30d', label: '30天' },
  { key: '1y', label: '1年' },
  { key: 'total', label: '全部' },
];

function formatTokens(value: number): string {
  return value.toLocaleString('zh-CN');
}

function formatRate(rate: number | null): string {
  if (rate === null) return '—';
  return `${(rate * 100).toFixed(1)}%`;
}

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm" style={{ color: 'var(--fg-secondary)' }}>{label}</span>
      <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>{value}</span>
    </div>
  );
}

function ModelRow({ usage, maxTotal }: { usage: ModelUsage; maxTotal: number }) {
  const width = maxTotal > 0 ? Math.max((usage.total_tokens / maxTotal) * 100, 1) : 0;
  const rate =
    usage.cache_hit_rate === null
      ? '—'
      : `${(usage.cache_hit_rate * 100).toFixed(1)}%`;
  return (
    <div className="px-4 py-3" style={{ borderBottom: '0.5px solid var(--border)' }}>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>
          {usage.model}
        </span>
        <span className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
          {formatTokens(usage.total_tokens)} tokens
        </span>
      </div>
      <div className="h-1.5 rounded-full overflow-hidden mb-2" style={{ background: 'var(--bg-button-secondary)' }}>
        <div
          className="h-full rounded-full"
          style={{ width: `${width}%`, background: 'var(--accent-green)' }}
        />
      </div>
      <div className="flex items-center justify-between text-xs" style={{ color: 'var(--fg-tertiary)' }}>
        <span>{usage.calls} 次调用 · 输入 {formatTokens(usage.input_tokens)} · 输出 {formatTokens(usage.output_tokens)}</span>
        <span>缓存命中 {rate}</span>
      </div>
    </div>
  );
}

export function UsageStatsSection() {
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [range, setRange] = useState<UsageRange>('1d');
  const [loading, setLoading] = useState(true);

  const loadStats = useCallback(async (selectedRange: UsageRange) => {
    try {
      setLoading(true);
      const data = await usageApi.stats(selectedRange);
      setStats(data);
    } catch {
      toast.error('加载用量统计失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadStats(range);
  }, [range, loadStats]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-5 w-5 animate-spin mr-2" style={{ color: 'var(--icon-accent)' }} />
        <span style={{ color: 'var(--fg-tertiary)' }}>加载用量统计中...</span>
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="flex items-center justify-center h-full">
        <Button variant="outline" onClick={() => loadStats(range)}>重新加载</Button>
      </div>
    );
  }

  const maxTotal = Math.max(1, ...stats.models.map(m => m.total_tokens));

  return (
    <div className="flex flex-col h-full" style={{ fontFamily: 'var(--font-sans)' }}>
      <div className="flex-shrink-0 px-6 pt-6 pb-4">
        <h1 className="text-2xl font-semibold mb-1 flex items-center gap-2" style={{ color: 'var(--fg-85)' }}>
          <Coins className="h-5 w-5" />
          用量统计
        </h1>
        <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
          查看各类模型的 Token 总消耗与缓存命中率
        </p>
      </div>

      <div className="flex-shrink-0 px-6 pb-3 flex items-center gap-1">
        {RANGES.map(({ key, label }) => (
          <Button
            key={key}
            variant={range === key ? 'default' : 'outline'}
            size="sm"
            onClick={() => setRange(key)}
          >
            {label}
          </Button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
        <div className="max-w-[760px] space-y-4">
          {/* 总体统计 */}
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center gap-2 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <BarChart3 className="h-4 w-4" style={{ color: 'var(--icon-accent)' }} />
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>总量</span>
            </div>
            <div className="px-4 py-3">
              <StatRow label="总调用次数" value={formatTokens(stats.totals.calls)} />
              <StatRow label="输入 Token" value={formatTokens(stats.totals.input_tokens)} />
              <StatRow label="输出 Token" value={formatTokens(stats.totals.output_tokens)} />
              <StatRow label="总消耗 Token" value={formatTokens(stats.totals.total_tokens)} />
              <StatRow label="缓存命中 Token" value={formatTokens(stats.totals.cache_hit_tokens)} />
              <StatRow label="缓存命中率" value={formatRate(stats.totals.cache_hit_rate)} />
            </div>
          </div>

          {/* 分模型统计 */}
          <div className="rounded-xl overflow-hidden" style={{ border: '0.5px solid var(--border)' }}>
            <div className="flex items-center gap-2 px-4 py-2.5" style={{ borderBottom: '0.5px solid var(--border)' }}>
              <Coins className="h-4 w-4" style={{ color: 'var(--icon-accent)' }} />
              <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>按模型</span>
            </div>
            {stats.models.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm" style={{ color: 'var(--fg-tertiary)' }}>
                暂无用量数据
              </div>
            ) : (
              stats.models.map(m => (
                <ModelRow key={m.model} usage={m} maxTotal={maxTotal} />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
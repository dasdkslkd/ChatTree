import { Bell, CheckCircle2, Loader2, XCircle } from 'lucide-react';
import type { TaskNotificationItem } from '../../../types/transcript';

function statusLabel(status: TaskNotificationItem['status']): string {
  if (status === 'unbound') return '待绑定';
  if (status === 'bound') return '已绑定';
  if (status === 'delivering') return '投递中';
  if (status === 'delivered') return '已投递';
  if (status === 'delivery_failed') return '投递失败';
  return '已取消';
}

function StatusIcon({ status }: { status: TaskNotificationItem['status'] }) {
  if (status === 'delivered') return <CheckCircle2 className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />;
  if (status === 'delivering') return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" style={{ color: 'var(--icon-accent)' }} />;
  if (status === 'delivery_failed' || status === 'delivery_cancelled') return <XCircle className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--destructive)' }} />;
  return <Bell className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />;
}

export function TaskNotificationCard({ item }: { item: TaskNotificationItem }) {
  return (
    <div className="transcript-task-notification w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-2 rounded-md px-3 py-2 text-sm"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-secondary)',
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex min-w-0 items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <StatusIcon status={item.status} />
          <span className="shrink-0">任务通知 · {statusLabel(item.status)}</span>
          <span className="min-w-0 truncate">{item.source_run_kind}</span>
        </div>
        {item.summary && (
          <div className="text-sm font-medium leading-5" style={{ color: 'var(--fg-primary)' }}>
            {item.summary}
          </div>
        )}
        {item.content && (
          <div className="whitespace-pre-wrap text-sm leading-6" style={{ color: 'var(--fg-secondary)' }}>
            {item.content}
          </div>
        )}
      </div>
    </div>
  );
}

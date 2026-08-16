export const ACTIVE_STREAM_VISIBLE_POLL_MS = 5000;

export function getActiveStreamPollingDelay(options: {
  activeStreamCount: number;
  documentHidden: boolean;
}): number | null {
  if (options.documentHidden) return null;
  return options.activeStreamCount > 0 ? ACTIVE_STREAM_VISIBLE_POLL_MS : null;
}


export function getStreamStatusText(status: string, errorMessage?: string | null): string | null {
  if (status === 'waiting_approval') return '等待工具审批';
  if (status === 'stopping') return '正在停止';
  if (status === 'error') return errorMessage?.trim() || '生成出错';
  if (status === 'stopped') return '已停止';
  return null;
}

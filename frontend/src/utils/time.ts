export function formatConversationTime(timestamp: number | undefined): string {
  if (!timestamp) return '';
  const timeMs = timestamp > 1_000_000_000_000 ? timestamp : timestamp * 1000;
  const diffMinutes = Math.max(0, Math.floor((Date.now() - timeMs) / 60000));
  if (diffMinutes < 1) return '刚刚';
  if (diffMinutes < 60) return `${diffMinutes} 分`;
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours} 小时`;
  const diffDays = Math.floor(diffHours / 24);
  return `${diffDays} 天`;
}

export function formatProcessedDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return null;

  const totalSeconds = Math.max(1, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];

  if (hours > 0) parts.push(`${hours}h`);
  if (hours > 0 || minutes > 0) parts.push(`${minutes}m`);
  parts.push(`${seconds}s`);
  return parts.join('');
}

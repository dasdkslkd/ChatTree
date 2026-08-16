// 本次运行中的错误消息历史：模块级内存存储，刷新页面即清空。
// 独立成纯模块（无 React、无 DOM 副作用），底部状态栏与 toast 收集入口共用。
export interface ErrorHistoryEntry {
  id: string;
  time: number;
  message: string;
}

const MAX_ENTRIES = 200;
const entries: ErrorHistoryEntry[] = [];
const listeners = new Set<() => void>();
let nextId = 0;

export function recordError(message: string): void {
  entries.push({ id: String(++nextId), time: Date.now(), message });
  if (entries.length > MAX_ENTRIES) entries.shift();
  for (const listener of listeners) listener();
}

export function getErrorHistory(): readonly ErrorHistoryEntry[] {
  return entries;
}

export function clearErrorHistory(): void {
  entries.length = 0;
  for (const listener of listeners) listener();
}

export function subscribeErrorHistory(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
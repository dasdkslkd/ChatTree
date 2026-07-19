export type PageLifecycleTarget = {
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
};

export type VisibilityLifecycleTarget = PageLifecycleTarget & {
  readonly visibilityState: string;
};

export function installPageLifecycleFlush(
  pageTarget: PageLifecycleTarget,
  visibilityTarget: VisibilityLifecycleTarget,
  flush: () => void,
): () => void {
  let disposed = false;
  const flushWhenHidden = () => {
    if (visibilityTarget.visibilityState === 'hidden') flush();
  };
  const flushForPageExit = () => flush();

  visibilityTarget.addEventListener('visibilitychange', flushWhenHidden);
  pageTarget.addEventListener('pagehide', flushForPageExit);
  pageTarget.addEventListener('beforeunload', flushForPageExit);

  return () => {
    if (disposed) return;
    disposed = true;
    visibilityTarget.removeEventListener('visibilitychange', flushWhenHidden);
    pageTarget.removeEventListener('pagehide', flushForPageExit);
    pageTarget.removeEventListener('beforeunload', flushForPageExit);
    flush();
  };
}

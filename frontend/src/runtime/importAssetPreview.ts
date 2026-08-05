export type ImportAssetBlobFetcher = (
  conversationId: string,
  filename: string,
  signal?: AbortSignal,
) => Promise<Blob>;

type ObjectUrlApi = Pick<typeof URL, 'createObjectURL' | 'revokeObjectURL'>;

type PreviewEntry = {
  controller: AbortController | null;
  promise: Promise<string | null> | null;
  url: string | null;
};

function assetKey(conversationId: string, filename: string): string {
  return JSON.stringify([conversationId, filename]);
}

export class ImportAssetPreviewCache {
  private readonly entries = new Map<string, PreviewEntry>();
  private readonly listeners = new Set<() => void>();
  private readonly fetchBlob: ImportAssetBlobFetcher;
  private readonly objectUrls: ObjectUrlApi;

  constructor(
    fetchBlob: ImportAssetBlobFetcher,
    objectUrls: ObjectUrlApi = URL,
  ) {
    this.fetchBlob = fetchBlob;
    this.objectUrls = objectUrls;
  }

  peek(conversationId: string, filename: string): string | null {
    return this.entries.get(assetKey(conversationId, filename))?.url ?? null;
  }

  load(conversationId: string, filename: string): Promise<string | null> {
    const key = assetKey(conversationId, filename);
    const existing = this.entries.get(key);
    if (existing?.url) return Promise.resolve(existing.url);
    if (existing?.promise) return existing.promise;
    if (existing) this.releaseEntry(key, existing, true);

    const controller = new AbortController();
    const entry: PreviewEntry = {
      controller,
      promise: null,
      url: null,
    };
    const promise = this.fetchBlob(conversationId, filename, controller.signal)
      .then((blob) => {
        if (this.entries.get(key) !== entry) return null;
        const url = this.objectUrls.createObjectURL(blob);
        if (this.entries.get(key) !== entry) {
          this.objectUrls.revokeObjectURL(url);
          return null;
        }
        entry.controller = null;
        entry.promise = null;
        entry.url = url;
        this.notify();
        return url;
      })
      .catch((error: unknown) => {
        const stillOwned = this.entries.get(key) === entry;
        if (stillOwned) this.entries.delete(key);
        if (!stillOwned || entry.controller?.signal.aborted) return null;
        throw error;
      });

    entry.promise = promise;
    this.entries.set(key, entry);
    return promise;
  }

  remove(conversationId: string, filename: string): void {
    const key = assetKey(conversationId, filename);
    const entry = this.entries.get(key);
    if (entry) this.releaseEntry(key, entry, true);
  }

  clear(): void {
    if (this.entries.size === 0) return;
    for (const [key, entry] of this.entries) {
      this.releaseEntry(key, entry, false);
    }
    this.notify();
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private releaseEntry(key: string, entry: PreviewEntry, shouldNotify: boolean): void {
    if (this.entries.get(key) !== entry) return;
    this.entries.delete(key);
    entry.controller?.abort();
    if (entry.url) this.objectUrls.revokeObjectURL(entry.url);
    if (shouldNotify) this.notify();
  }

  private notify(): void {
    for (const listener of this.listeners) listener();
  }
}

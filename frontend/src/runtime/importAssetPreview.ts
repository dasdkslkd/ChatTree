import type { ConnectionEpochRuntime, ConnectionEpochToken } from './connectionEpoch';

export type ImportAssetBlobFetcher = (
  conversationId: string,
  filename: string,
  token: ConnectionEpochToken,
  signal?: AbortSignal,
) => Promise<Blob>;

type PreviewEpochSource = Pick<ConnectionEpochRuntime, 'isCurrent'>;

type ObjectUrlApi = Pick<typeof URL, 'createObjectURL' | 'revokeObjectURL'>;

type PreviewEntry = {
  token: ConnectionEpochToken;
  controller: AbortController | null;
  promise: Promise<string | null> | null;
  url: string | null;
};

function assetKey(conversationId: string, filename: string): string {
  return JSON.stringify([conversationId, filename]);
}

export type ImportAssetMutation = Readonly<{
  key: string;
  generation: number;
}>;

export class ImportAssetMutationOwner {
  private readonly generations = new Map<string, number>();
  private nextGeneration = 0;

  begin(conversationId: string, filename: string): ImportAssetMutation {
    const key = assetKey(conversationId, filename);
    const generation = ++this.nextGeneration;
    this.generations.set(key, generation);
    return Object.freeze({ key, generation });
  }

  claim(
    mutation: ImportAssetMutation,
    conversationId: string,
    filename: string,
  ): ImportAssetMutation | null {
    if (!this.owns(mutation)) return null;
    const key = assetKey(conversationId, filename);
    const latest = this.generations.get(key);
    if (latest !== undefined && latest > mutation.generation) return null;
    this.generations.set(key, mutation.generation);
    return Object.freeze({ key, generation: mutation.generation });
  }

  owns(mutation: ImportAssetMutation): boolean {
    return this.generations.get(mutation.key) === mutation.generation;
  }

  clear(): void {
    this.generations.clear();
  }
}

export class ImportAssetMutationQueue {
  private readonly tails = new Map<string, Promise<void>>();

  run<T>(
    conversationId: string,
    filename: string,
    operation: () => Promise<T>,
  ): Promise<T> {
    const key = assetKey(conversationId, filename);
    const previous = this.tails.get(key) ?? Promise.resolve();
    const result = previous.then(operation);
    const tail = result.then(
      () => undefined,
      () => undefined,
    );
    this.tails.set(key, tail);
    void tail.then(() => {
      if (this.tails.get(key) === tail) this.tails.delete(key);
    });
    return result;
  }
}

function sameToken(left: ConnectionEpochToken, right: ConnectionEpochToken): boolean {
  return left.profileId === right.profileId
    && left.serverInstanceId === right.serverInstanceId
    && left.connectionEpoch === right.connectionEpoch
    && left.connectionLeaseId === right.connectionLeaseId
    && left.generation === right.generation;
}

export class ImportAssetPreviewCache {
  private readonly entries = new Map<string, PreviewEntry>();
  private readonly listeners = new Set<() => void>();
  private readonly fetchBlob: ImportAssetBlobFetcher;
  private readonly epochSource: PreviewEpochSource;
  private readonly objectUrls: ObjectUrlApi;

  constructor(
    fetchBlob: ImportAssetBlobFetcher,
    epochSource: PreviewEpochSource,
    objectUrls: ObjectUrlApi = URL,
  ) {
    this.fetchBlob = fetchBlob;
    this.epochSource = epochSource;
    this.objectUrls = objectUrls;
  }

  peek(conversationId: string, filename: string): string | null {
    const entry = this.entries.get(assetKey(conversationId, filename));
    if (!entry || !this.epochSource.isCurrent(entry.token)) return null;
    return entry.url;
  }

  load(
    conversationId: string,
    filename: string,
    token: ConnectionEpochToken,
  ): Promise<string | null> {
    if (!this.epochSource.isCurrent(token)) return Promise.resolve(null);

    const key = assetKey(conversationId, filename);
    const existing = this.entries.get(key);
    if (existing && sameToken(existing.token, token)) {
      if (existing.url) return Promise.resolve(existing.url);
      if (existing.promise) return existing.promise;
    }
    if (existing) this.releaseEntry(key, existing, true);

    const controller = new AbortController();
    const entry: PreviewEntry = {
      token,
      controller,
      promise: null,
      url: null,
    };
    const promise = this.fetchBlob(conversationId, filename, token, controller.signal)
      .then((blob) => {
        if (this.entries.get(key) !== entry || !this.epochSource.isCurrent(token)) return null;
        const url = this.objectUrls.createObjectURL(blob);
        if (this.entries.get(key) !== entry || !this.epochSource.isCurrent(token)) {
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
        if (!stillOwned || entry.controller?.signal.aborted || !this.epochSource.isCurrent(token)) {
          return null;
        }
        throw error;
      });

    entry.promise = promise;
    this.entries.set(key, entry);
    return promise;
  }

  installFile(
    conversationId: string,
    filename: string,
    file: File,
    token: ConnectionEpochToken,
  ): string | null {
    if (!this.epochSource.isCurrent(token)) return null;
    const key = assetKey(conversationId, filename);
    const existing = this.entries.get(key);
    if (existing) this.releaseEntry(key, existing, false);

    const url = this.objectUrls.createObjectURL(file);
    if (!this.epochSource.isCurrent(token)) {
      this.objectUrls.revokeObjectURL(url);
      return null;
    }
    this.entries.set(key, {
      token,
      controller: null,
      promise: null,
      url,
    });
    this.notify();
    return url;
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

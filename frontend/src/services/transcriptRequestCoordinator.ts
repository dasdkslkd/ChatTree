import type { TranscriptSnapshot } from '../types/transcript';

export type TranscriptRequestTarget = {
  conversationId: string;
  tipNodeId: string;
};

type TranscriptRequestCoordinatorOptions = {
  fetchSnapshot: (
    conversationId: string,
    tipNodeId: string,
    signal: AbortSignal,
  ) => Promise<TranscriptSnapshot>;
  getVisibleTarget: () => TranscriptRequestTarget | null;
  onLoadingChange: (loading: boolean) => void;
  onSnapshot: (snapshot: TranscriptSnapshot) => void;
  onErrorChange: (error: unknown | null) => void;
};

type ActiveTranscriptRequest = TranscriptRequestTarget & {
  controller: AbortController;
  promise: Promise<void>;
};

function targetsMatch(
  left: TranscriptRequestTarget | null,
  right: TranscriptRequestTarget,
): boolean {
  return left?.conversationId === right.conversationId
    && left.tipNodeId === right.tipNodeId;
}

function isCancellationError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const candidate = error as { name?: string; code?: string };
  return candidate.name === 'AbortError' || candidate.code === 'ERR_CANCELED';
}

export function createTranscriptRequestCoordinator(
  options: TranscriptRequestCoordinatorOptions,
) {
  let active: ActiveTranscriptRequest | null = null;

  const isVisible = (request: TranscriptRequestTarget) => (
    targetsMatch(options.getVisibleTarget(), request)
  );

  const ownsRequest = (request: ActiveTranscriptRequest) => active === request;

  const canCommit = (request: ActiveTranscriptRequest) => (
    !request.controller.signal.aborted
    && ownsRequest(request)
    && isVisible(request)
  );

  const cancelActive = () => {
    const request = active;
    if (!request) return;
    active = null;
    request.controller.abort();
  };

  const request = (target: TranscriptRequestTarget): Promise<void> => {
    if (active && targetsMatch(active, target)) {
      if (isVisible(active)) options.onLoadingChange(true);
      return active.promise;
    }

    cancelActive();

    const controller = new AbortController();
    const current = {
      ...target,
      controller,
      promise: Promise.resolve(),
    } satisfies ActiveTranscriptRequest;
    active = current;

    current.promise = Promise.resolve()
      .then(() => options.fetchSnapshot(
        current.conversationId,
        current.tipNodeId,
        controller.signal,
      ))
      .then((snapshot) => {
        if (!canCommit(current)) return;
        if (
          snapshot.conversation_id !== current.conversationId
          || snapshot.tip_node_id !== current.tipNodeId
        ) return;
        options.onSnapshot(snapshot);
        if (canCommit(current)) options.onErrorChange(null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isCancellationError(error)) return;
        if (canCommit(current)) options.onErrorChange(error);
      })
      .finally(() => {
        controller.abort();
        if (!ownsRequest(current)) return;
        active = null;
        if (isVisible(current)) options.onLoadingChange(false);
      });

    if (canCommit(current)) options.onLoadingChange(true);
    if (canCommit(current)) options.onErrorChange(null);
    return current.promise;
  };

  return { request, cancelActive };
}

export type TranscriptRequestCoordinator = ReturnType<typeof createTranscriptRequestCoordinator>;

import type { TranscriptSnapshot } from '../types/transcript';
import {
  composeConnectionAbortSignal,
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

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
  epochSource?: TranscriptRequestEpochSource;
};

type ActiveTranscriptRequest = TranscriptRequestTarget & {
  controller: AbortController;
  epochToken: ConnectionEpochToken;
  promise: Promise<void>;
};

export type TranscriptRequestEpochSource = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor'
>;

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
  const epochSource = options.epochSource ?? connectionEpochRuntime;

  const isVisible = (request: TranscriptRequestTarget) => (
    targetsMatch(options.getVisibleTarget(), request)
  );

  const ownsRequest = (request: ActiveTranscriptRequest) => active === request;

  const isCurrent = (request: ActiveTranscriptRequest) => (
    epochSource.isCurrent(request.epochToken)
  );

  const canCommit = (request: ActiveTranscriptRequest) => (
    !request.controller.signal.aborted
    && isCurrent(request)
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
      if (isCurrent(active) && isVisible(active)) {
        options.onLoadingChange(true);
      }
      return active.promise;
    }

    cancelActive();

    let epochToken: ConnectionEpochToken;
    try {
      epochToken = epochSource.capture();
    } catch {
      return Promise.resolve();
    }
    if (!epochSource.isCurrent(epochToken)) return Promise.resolve();

    const controller = new AbortController();
    const signal = composeConnectionAbortSignal(
      controller.signal,
      epochSource.signalFor(epochToken),
    );
    const current = {
      ...target,
      controller,
      epochToken,
      promise: Promise.resolve(),
    } satisfies ActiveTranscriptRequest;
    active = current;

    current.promise = Promise.resolve()
      .then(() => {
        if (!isCurrent(current)) return null;
        return options.fetchSnapshot(
          current.conversationId,
          current.tipNodeId,
          signal,
        );
      })
      .then((snapshot) => {
        if (!snapshot || !canCommit(current)) return;
        if (
          snapshot.conversation_id !== current.conversationId
          || snapshot.tip_node_id !== current.tipNodeId
        ) return;
        options.onSnapshot(snapshot);
        if (canCommit(current)) options.onErrorChange(null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isCancellationError(error)) return;
        if (!canCommit(current)) return;
        options.onErrorChange(error);
      })
      .finally(() => {
        controller.abort();
        if (!ownsRequest(current)) return;
        active = null;
        if (isCurrent(current) && isVisible(current)) {
          options.onLoadingChange(false);
        }
      });

    if (canCommit(current)) options.onLoadingChange(true);
    if (canCommit(current)) options.onErrorChange(null);

    return current.promise;
  };

  return { request, cancelActive };
}

export type TranscriptRequestCoordinator = ReturnType<typeof createTranscriptRequestCoordinator>;

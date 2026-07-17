import {
  connectionEpochRuntime,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';

export type ConversationSyncInclude =
  | 'messages'
  | 'branches'
  | 'transcript'
  | 'conversations'
  | 'tree'
  | 'taskState'
  | 'plan'
  | 'sideRuns';

export type MessageRefreshOptions = {
  awaitNodeId?: string;
  awaitRole?: 'assistant' | 'user';
  retries?: number;
};

export type ConversationSyncRequest = {
  reason?: string;
  include?: ConversationSyncInclude[];
  awaitNodeId?: string;
  awaitRole?: 'assistant' | 'user';
  messageRetries?: number;
};

export type ConversationSyncResult = {
  messagesConfirmed: boolean;
};

export type ConversationSyncOperations = {
  refreshMessages: (conversationId: string, opts?: MessageRefreshOptions) => Promise<boolean>;
  refreshBranches: (conversationId: string) => Promise<boolean>;
  refreshTranscript: (conversationId: string) => Promise<void>;
  loadConversations: () => Promise<void>;
  loadTree: (conversationId: string) => Promise<void>;
  refreshTaskState: (conversationId: string) => Promise<unknown>;
  refreshActivePlan: (conversationId: string) => Promise<unknown>;
  syncSideRuns: (conversationId: string) => Promise<void>;
};

type Waiter = {
  resolve: (result: ConversationSyncResult) => void;
};

type PendingSync = {
  include: Set<ConversationSyncInclude>;
  messageRequests: MessageRefreshOptions[];
  waiters: Waiter[];
  reasons: Set<string>;
};

type ConversationState = {
  running: boolean;
  pending: PendingSync | null;
  epochToken: ConnectionEpochToken;
};

export type ConversationSyncEpochSource = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor'
>;

const DEFAULT_INCLUDE: ConversationSyncInclude[] = ['messages'];

function createPending(): PendingSync {
  return {
    include: new Set(),
    messageRequests: [],
    waiters: [],
    reasons: new Set(),
  };
}

function messageRequestKey(options: MessageRefreshOptions): string {
  return [
    options.awaitNodeId ?? '',
    options.awaitRole ?? '',
    String(options.retries ?? ''),
  ].join('\u0000');
}

function mergeMessageRequest(target: MessageRefreshOptions[], options: MessageRefreshOptions): void {
  const hasSpecificRequest = options.awaitNodeId || options.awaitRole;
  if (!hasSpecificRequest && target.some((request) => request.awaitNodeId || request.awaitRole)) {
    return;
  }
  if (hasSpecificRequest) {
    for (let index = target.length - 1; index >= 0; index -= 1) {
      const request = target[index];
      if (!request.awaitNodeId && !request.awaitRole) target.splice(index, 1);
    }
  }

  const key = messageRequestKey(options);
  if (target.some((request) => messageRequestKey(request) === key)) return;
  target.push(options);
}

function mergeRequest(pending: PendingSync, request: ConversationSyncRequest): void {
  const include = request.include?.length ? request.include : DEFAULT_INCLUDE;
  for (const item of include) pending.include.add(item);
  if (request.awaitNodeId) pending.include.add('messages');
  if (request.reason) pending.reasons.add(request.reason);
  if (pending.include.has('messages') || request.awaitNodeId) {
    mergeMessageRequest(pending.messageRequests, {
      awaitNodeId: request.awaitNodeId,
      awaitRole: request.awaitRole,
      retries: request.messageRetries,
    });
  }
}

export class ConversationSyncCoordinator {
  private operations: ConversationSyncOperations | null;

  private readonly states = new Map<string, ConversationState>();

  private readonly epochSource: ConversationSyncEpochSource;

  constructor(
    operations: ConversationSyncOperations | null = null,
    epochSource: ConversationSyncEpochSource = connectionEpochRuntime,
  ) {
    this.operations = operations;
    this.epochSource = epochSource;
  }

  setOperations(operations: ConversationSyncOperations): void {
    this.operations = operations;
  }

  schedule(conversationId: string, request: ConversationSyncRequest): Promise<ConversationSyncResult> {
    if (!this.operations) {
      return Promise.resolve({ messagesConfirmed: false });
    }

    let state = this.states.get(conversationId);
    if (!state) {
      let epochToken: ConnectionEpochToken;
      try {
        epochToken = this.epochSource.capture();
      } catch {
        return Promise.resolve({ messagesConfirmed: false });
      }
      state = { running: false, pending: null, epochToken };
      this.states.set(conversationId, state);
    }
    if (!this.epochSource.isCurrent(state.epochToken)) {
      return Promise.resolve({ messagesConfirmed: false });
    }
    if (!state.pending) state.pending = createPending();
    mergeRequest(state.pending, request);

    const promise = new Promise<ConversationSyncResult>((resolve) => {
      state?.pending?.waiters.push({ resolve });
    });

    if (!state.running) {
      state.running = true;
      void Promise.resolve().then(() => this.drain(conversationId, state));
    }
    return promise;
  }

  private async drain(conversationId: string, state: ConversationState): Promise<void> {
    try {
      while (state.pending && this.epochSource.isCurrent(state.epochToken)) {
        const pending = state.pending;
        state.pending = null;
        const result = await this.runPending(conversationId, pending, state.epochToken);
        const delivered = this.epochSource.isCurrent(state.epochToken)
          ? result
          : { messagesConfirmed: false };
        for (const waiter of pending.waiters) waiter.resolve(delivered);
      }
    } finally {
      state.running = false;
      if (state.pending && this.epochSource.isCurrent(state.epochToken)) {
        state.running = true;
        void Promise.resolve().then(() => this.drain(conversationId, state));
      } else {
        if (state.pending) {
          for (const waiter of state.pending.waiters) {
            waiter.resolve({ messagesConfirmed: false });
          }
          state.pending = null;
        }
        if (this.states.get(conversationId) === state) {
          this.states.delete(conversationId);
        }
      }
    }
  }

  private async runPending(
    conversationId: string,
    pending: PendingSync,
    epochToken: ConnectionEpochToken,
  ): Promise<ConversationSyncResult> {
    const operations = this.operations;
    if (!operations || !this.epochSource.isCurrent(epochToken)) {
      return { messagesConfirmed: false };
    }

    let messagesConfirmed = true;
    if (pending.include.has('messages')) {
      const requests = pending.messageRequests.length > 0 ? pending.messageRequests : [{}];
      for (const request of requests) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
        let ok = false;
        try {
          ok = await operations.refreshMessages(conversationId, request);
        } catch {
          ok = false;
        }
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
        if (!ok) messagesConfirmed = false;
      }
    }

    if (pending.include.has('tree')) {
      if (!await this.runBestEffort(epochToken, () => operations.loadTree(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('branches')) {
      if (!await this.runBestEffort(epochToken, () => operations.refreshBranches(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('transcript')) {
      if (!await this.runBestEffort(epochToken, () => operations.refreshTranscript(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('conversations')) {
      if (!await this.runBestEffort(epochToken, () => operations.loadConversations())) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('taskState')) {
      if (!await this.runBestEffort(epochToken, () => operations.refreshTaskState(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('plan')) {
      if (!await this.runBestEffort(epochToken, () => operations.refreshActivePlan(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }
    if (pending.include.has('sideRuns')) {
      if (!await this.runBestEffort(epochToken, () => operations.syncSideRuns(conversationId))) {
        if (!this.epochSource.isCurrent(epochToken)) return { messagesConfirmed: false };
      }
    }

    return {
      messagesConfirmed: this.epochSource.isCurrent(epochToken) && messagesConfirmed,
    };
  }

  private async runBestEffort(
    epochToken: ConnectionEpochToken,
    operation: () => Promise<unknown>,
  ): Promise<boolean> {
    if (!this.epochSource.isCurrent(epochToken)) return false;
    try {
      await operation();
      return this.epochSource.isCurrent(epochToken);
    } catch {
      return false;
    }
  }
}

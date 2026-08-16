export type ConversationSyncInclude =
  | 'messages'
  | 'transcript'
  | 'conversations'
  | 'tree'
  | 'taskState';

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
  refreshTranscript: (conversationId: string) => Promise<void>;
  loadConversations: () => Promise<void>;
  loadTree: (conversationId: string) => Promise<void>;
  refreshTaskState: (conversationId: string) => Promise<unknown>;
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
};

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
  if (hasSpecificRequest) {
    // 同 tick 只保留最新 specific 请求：branch 快照含整条分支线，最新落盘即覆盖前面请求的确认目标
    target.length = 0;
    target.push(options);
    return;
  }
  if (target.some((request) => request.awaitNodeId || request.awaitRole)) return;
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

async function runBestEffort(operation: () => Promise<unknown>): Promise<boolean> {
  try {
    await operation();
    return true;
  } catch {
    return false;
  }
}

export class ConversationSyncCoordinator {
  private operations: ConversationSyncOperations | null;

  private readonly states = new Map<string, ConversationState>();

  constructor(operations: ConversationSyncOperations | null = null) {
    this.operations = operations;
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
      state = { running: false, pending: null };
      this.states.set(conversationId, state);
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
      while (state.pending) {
        const pending = state.pending;
        state.pending = null;
        const result = await this.runPending(conversationId, pending);
        for (const waiter of pending.waiters) waiter.resolve(result);
      }
    } finally {
      state.running = false;
      if (state.pending) {
        state.running = true;
        void Promise.resolve().then(() => this.drain(conversationId, state));
      } else {
        this.states.delete(conversationId);
      }
    }
  }

  private async runPending(conversationId: string, pending: PendingSync): Promise<ConversationSyncResult> {
    const operations = this.operations;
    if (!operations) return { messagesConfirmed: false };

    let messagesConfirmed = true;
    if (pending.include.has('messages')) {
      const requests = pending.messageRequests.length > 0 ? pending.messageRequests : [{}];
      for (const request of requests) {
        let ok = false;
        try {
          ok = await operations.refreshMessages(conversationId, request);
        } catch {
          ok = false;
        }
        if (!ok) messagesConfirmed = false;
      }
    }

    const parallel: Promise<boolean>[] = [];
    if (pending.include.has('tree')) {
      parallel.push(runBestEffort(() => operations.loadTree(conversationId)));
    }
    if (pending.include.has('transcript')) {
      parallel.push(runBestEffort(() => operations.refreshTranscript(conversationId)));
    }
    if (pending.include.has('conversations')) {
      parallel.push(runBestEffort(() => operations.loadConversations()));
    }
    if (pending.include.has('taskState')) {
      parallel.push(runBestEffort(() => operations.refreshTaskState(conversationId)));
    }
    await Promise.allSettled(parallel);
    return { messagesConfirmed };
  }
}

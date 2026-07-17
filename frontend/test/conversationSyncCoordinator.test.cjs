const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  ConversationSyncCoordinator,
} = require(path.join(__dirname, '../src/services/conversationSyncCoordinator.ts'));

function createOperations(calls, overrides = {}) {
  return {
    refreshMessages: async (_conversationId, options = {}) => {
      calls.push(`messages:${options.awaitNodeId || ''}:${options.awaitRole || ''}:${options.retries ?? ''}`);
      return true;
    },
    refreshBranches: async () => {
      calls.push('branches');
      return true;
    },
    refreshTranscript: async () => {
      calls.push('transcript');
    },
    loadConversations: async () => {
      calls.push('conversations');
    },
    loadTree: async () => {
      calls.push('tree');
    },
    refreshTaskState: async () => {
      calls.push('taskState');
    },
    refreshActivePlan: async () => {
      calls.push('plan');
    },
    syncSideRuns: async () => {
      calls.push('sideRuns');
    },
    ...overrides,
  };
}

function createEpochSource() {
  const token = Object.freeze({
    profileId: 'profile-a',
    serverInstanceId: '11111111-1111-4111-8111-111111111111',
    connectionEpoch: 1,
    connectionLeaseId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    generation: 1,
  });
  const controller = new AbortController();
  let current = true;
  let captures = 0;
  return {
    token,
    source: {
      capture() {
        captures += 1;
        return token;
      },
      isCurrent(candidate) {
        return current && candidate === token;
      },
      signalFor(candidate) {
        return current && candidate === token ? controller.signal : AbortSignal.abort();
      },
    },
    invalidate() {
      current = false;
      controller.abort();
    },
    get captures() {
      return captures;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function testCoalescesSameTickRequests() {
  const calls = [];
  const coordinator = new ConversationSyncCoordinator(
    createOperations(calls),
    createEpochSource().source,
  );
  const first = coordinator.schedule('conv-1', {
    reason: 'first',
    include: ['messages', 'branches', 'transcript'],
    awaitNodeId: 'node-1',
    messageRetries: 6,
  });
  const second = coordinator.schedule('conv-1', {
    reason: 'second',
    include: ['messages', 'branches', 'conversations', 'taskState'],
    awaitNodeId: 'node-1',
    messageRetries: 6,
  });
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.equal(firstResult.messagesConfirmed, true);
  assert.equal(secondResult.messagesConfirmed, true);
  assert.deepEqual(calls, [
    'messages:node-1::6',
    'branches',
    'transcript',
    'conversations',
    'taskState',
  ]);
}

async function testSpecificMessageRequestSuppressesGenericDuplicate() {
  const calls = [];
  const coordinator = new ConversationSyncCoordinator(
    createOperations(calls),
    createEpochSource().source,
  );
  await Promise.all([
    coordinator.schedule('conv-1', {
      include: ['messages'],
      awaitNodeId: 'node-1',
      messageRetries: 0,
    }),
    coordinator.schedule('conv-1', {
      include: ['messages', 'branches', 'transcript'],
      messageRetries: 0,
    }),
  ]);
  assert.deepEqual(calls, [
    'messages:node-1::0',
    'branches',
    'transcript',
  ]);
}

async function testSchedulesNextRoundWhileRunning() {
  const calls = [];
  let releaseMessages;
  const messagesBlocked = new Promise((resolve) => {
    releaseMessages = resolve;
  });
  const coordinator = new ConversationSyncCoordinator(createOperations(calls, {
    refreshMessages: async () => {
      calls.push('messages');
      await messagesBlocked;
      return true;
    },
  }), createEpochSource().source);
  const first = coordinator.schedule('conv-1', { include: ['messages'] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = coordinator.schedule('conv-1', { include: ['transcript'] });
  releaseMessages();
  await Promise.all([first, second]);
  assert.deepEqual(calls, ['messages', 'transcript']);
}

async function testInvalidatedOwnershipSettlesWaitersWithoutLaterOperationsOrFutureCapture() {
  const calls = [];
  const blocked = deferred();
  const epoch = createEpochSource();
  const coordinator = new ConversationSyncCoordinator(createOperations(calls, {
    refreshMessages: async () => {
      calls.push('messages');
      await blocked.promise;
      return true;
    },
  }), epoch.source);

  const first = coordinator.schedule('conv-1', {
    include: ['messages', 'branches', 'transcript'],
  });
  await new Promise((resolve) => setImmediate(resolve));
  epoch.invalidate();
  const second = coordinator.schedule('conv-1', {
    include: ['tree', 'sideRuns'],
  });
  blocked.resolve();

  assert.deepEqual(await Promise.all([first, second]), [
    { messagesConfirmed: false },
    { messagesConfirmed: false },
  ]);
  assert.deepEqual(calls, ['messages']);
  assert.equal(epoch.captures, 1);
}

async function testWaiterResultIsRecheckedAfterRunPendingSettles() {
  const epoch = createEpochSource();
  let checks = 0;
  const originalIsCurrent = epoch.source.isCurrent;
  epoch.source.isCurrent = (candidate) => {
    checks += 1;
    const current = originalIsCurrent(candidate);
    if (checks === 6) queueMicrotask(() => epoch.invalidate());
    return current;
  };
  const coordinator = new ConversationSyncCoordinator(
    createOperations([]),
    epoch.source,
  );

  const result = await coordinator.schedule('conv-1', { include: ['messages'] });
  assert.ok(checks >= 6, 'test must invalidate after runPending finalizes its result');
  assert.deepEqual(result, { messagesConfirmed: false });
}

(async () => {
  await testCoalescesSameTickRequests();
  await testSpecificMessageRequestSuppressesGenericDuplicate();
  await testSchedulesNextRoundWhileRunning();
  await testInvalidatedOwnershipSettlesWaitersWithoutLaterOperationsOrFutureCapture();
  await testWaiterResultIsRecheckedAfterRunPendingSettles();
  console.log('conversation sync coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

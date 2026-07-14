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

async function testCoalescesSameTickRequests() {
  const calls = [];
  const coordinator = new ConversationSyncCoordinator(createOperations(calls));
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
  const coordinator = new ConversationSyncCoordinator(createOperations(calls));
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
  }));
  const first = coordinator.schedule('conv-1', { include: ['messages'] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = coordinator.schedule('conv-1', { include: ['transcript'] });
  releaseMessages();
  await Promise.all([first, second]);
  assert.deepEqual(calls, ['messages', 'transcript']);
}

(async () => {
  await testCoalescesSameTickRequests();
  await testSpecificMessageRequestSuppressesGenericDuplicate();
  await testSchedulesNextRoundWhileRunning();
  console.log('conversation sync coordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

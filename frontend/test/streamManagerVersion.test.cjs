const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
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

class RetryableApiError extends Error {
  constructor(message) {
    super(message);
    this.retryable = true;
  }
}

const calls = { start: 0, attach: [] };
const messageApi = {
  async startRun() {
    calls.start += 1;
    return { run_id: 'run-1', created: true, status: 'running' };
  },
};
const runsApi = {
  attach: null,
};

const originalLoad = Module._load;
const originalWindow = globalThis.window;
Module._load = function loadStub(request, parent, isMain) {
  if (request === '../api/message') return { messageApi };
  if (request === '../api/runs') return { runsApi };
  if (request === '../api/errors') return { ChatTreeApiError: RetryableApiError };
  if (request === './slashRegistry') return { slashRegistry: { match: () => null } };
  if (request === '../perf/client') return { flushPerfEvents: async () => {} };
  if (request === '../perf/marks') {
    return { perfNow: () => 0, recordMark() {}, recordSpan() {} };
  }
  return originalLoad.call(this, request, parent, isMain);
};

globalThis.window = {
  setInterval: () => 1,
  clearInterval() {},
  setTimeout(callback) {
    queueMicrotask(callback);
    return 1;
  },
  clearTimeout() {},
};

function makePatch(runId, status, revision) {
  return {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision,
    operations: [{
      op: 'upsert',
      index: 0,
      item: { type: 'run_status', run_id: runId, status },
    }],
  };
}

function tick() {
  return new Promise((resolve) => setImmediate(resolve));
}

async function testSnapshotCachedUntilVersionChanges(StreamManager) {
  let release;
  calls.attach.length = 0;
  runsApi.attach = async function* attach(runId) {
    calls.attach.push(runId);
    yield makePatch(runId, 'running', 1);
    yield makePatch(runId, 'running', 2);
    await new Promise((resolve) => { release = resolve; });
  };
  const manager = new StreamManager();
  const started = manager.startStream('conv-1', { content: 'hello' }, 'hello');
  await tick();
  await tick();
  const first = manager.getConversationStates('conv-1');
  const second = manager.getConversationStates('conv-1');
  assert.equal(first, second, 'unchanged conversation states must share the cached array reference');
  assert.equal(first.length, 1, 'exactly one stream state must exist');
  assert.equal(first[0].version, 3, 'two patches must bump version from 1 to 3');
  release();
  await started;
}

async function testStreamStateHasNoDurationField(StreamManager) {
  calls.attach.length = 0;
  runsApi.attach = async function* attach(runId) {
    calls.attach.push(runId);
    yield makePatch(runId, 'error', 1);
  };
  const manager = new StreamManager();
  await manager.startStream('conv-1', { content: 'hello' }, 'hello');
  const state = manager.getConversationStates('conv-1')[0];
  assert.equal(state.status, 'error', 'error patch must leave the run in error state');
  assert.equal(state.duration, undefined, 'StreamState must not carry a duration field');
  assert.equal(Object.hasOwn(state, 'duration'), false, 'StreamState must not own a duration field');
}

async function testSubscribeReceivesConversationId(StreamManager) {
  const seen = [];
  calls.attach.length = 0;
  runsApi.attach = async function* attach(runId) {
    calls.attach.push(runId);
    yield makePatch(runId, 'completed', 1);
  };
  const manager = new StreamManager();
  const unsubscribe = manager.subscribe((conversationId) => seen.push(conversationId));
  try {
    await manager.startStream('conv-1', { content: 'hello' }, 'hello');
    assert.ok(seen.length > 0, 'subscriber must be notified at least once');
    assert.ok(seen.every((id) => id === 'conv-1'), `all notifications must carry conv-1, got ${JSON.stringify(seen)}`);
  } finally {
    unsubscribe();
  }
}

async function main() {
  try {
    const { StreamManager } = require(path.join(__dirname, '../src/services/streamManager.ts'));
    await testSnapshotCachedUntilVersionChanges(StreamManager);
    await testStreamStateHasNoDurationField(StreamManager);
    await testSubscribeReceivesConversationId(StreamManager);
    console.log('streamManager version/no-duration/subscribe tests passed');
  } finally {
    Module._load = originalLoad;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
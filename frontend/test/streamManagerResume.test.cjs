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

const calls = { start: 0, attach: [], status: [] };
const messageApi = {
  async startRun() {
    calls.start += 1;
    return { run_id: 'run-1', created: true, status: 'running' };
  },
};
const runsApi = {
  async get(runId) {
    calls.status.push(runId);
    return { run_id: runId, status: 'running' };
  },
  async *attach(runId) {
    calls.attach.push(runId);
    if (calls.attach.length === 1) throw new RetryableApiError('stream dropped');
    yield {
      type: 'transcript_patch',
      conversation_id: 'conv-1',
      node_id: 'node-1',
      revision: 1,
      operations: [{
        op: 'upsert',
        index: 0,
        item: { type: 'run_status', run_id: runId, status: 'completed' },
      }],
    };
  },
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

async function main() {
  try {
    const { StreamManager } = require(path.join(__dirname, '../src/services/streamManager.ts'));
    const manager = new StreamManager();

    await manager.startStream('conv-1', { content: 'hello' }, 'hello');

    assert.equal(calls.start, 1, 'transport reconnect must not resubmit the user message');
    assert.deepEqual(calls.attach, ['run-1', 'run-1']);
    assert.deepEqual(calls.status, ['run-1']);
    assert.equal(manager.getConversationStates('conv-1')[0].status, 'completed');
    console.log('streamManager resume tests passed');
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

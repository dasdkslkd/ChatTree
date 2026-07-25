const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  module._compile(output, filename);
};

const {
  createTranscriptRequestCoordinator,
} = require('../src/services/transcriptRequestCoordinator.ts');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function snapshot(conversationId, tipNodeId, revision = 1) {
  return {
    conversation_id: conversationId,
    node_id: tipNodeId,
    revision,
    items: [],
  };
}

function harness(fetchSnapshot) {
  let visible = { conversationId: 'conv-1', tipNodeId: 'node-1' };
  const loading = [];
  const snapshots = [];
  const errors = [];
  const coordinator = createTranscriptRequestCoordinator({
    fetchSnapshot,
    getVisibleTarget: () => visible,
    onLoadingChange: (value) => loading.push(value),
    onSnapshot: (value) => snapshots.push(value),
    onErrorChange: (value) => errors.push(value),
  });
  return {
    coordinator,
    loading,
    snapshots,
    errors,
    setVisible(value) {
      visible = value;
    },
  };
}

async function testSameVisibleTargetCoalescesOneRequest() {
  const pending = deferred();
  const calls = [];
  const state = harness(async (...args) => {
    calls.push(args);
    return pending.promise;
  });
  const target = { conversationId: 'conv-1', tipNodeId: 'node-1' };

  const first = state.coordinator.request(target);
  const second = state.coordinator.request(target);
  assert.equal(first, second);
  await Promise.resolve();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2] instanceof AbortSignal, true);

  pending.resolve(snapshot('conv-1', 'node-1'));
  await first;
  assert.equal(state.snapshots.length, 1);
  assert.equal(state.errors.at(-1), null);
  assert.equal(state.loading.at(-1), false);
}

async function testNewTargetAbortsOldAndOnlyLatestCanCommit() {
  const firstPending = deferred();
  const secondPending = deferred();
  const signals = [];
  const state = harness(async (_conversationId, tipNodeId, signal) => {
    signals.push(signal);
    return tipNodeId === 'node-1' ? firstPending.promise : secondPending.promise;
  });

  const first = state.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-1',
  });
  await Promise.resolve();
  state.setVisible({ conversationId: 'conv-1', tipNodeId: 'node-2' });
  const second = state.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-2',
  });
  await Promise.resolve();
  assert.equal(signals[0].aborted, true);
  assert.equal(signals[1].aborted, false);

  firstPending.resolve(snapshot('conv-1', 'node-1'));
  secondPending.resolve(snapshot('conv-1', 'node-2', 2));
  await Promise.all([first, second]);

  assert.deepEqual(
    state.snapshots.map((item) => item.node_id),
    ['node-2'],
  );
}

async function testMismatchedSnapshotAndHiddenTargetCannotCommit() {
  const state = harness(async () => snapshot('conv-1', 'wrong-node'));
  await state.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-1',
  });
  assert.deepEqual(state.snapshots, []);

  const pending = deferred();
  const hidden = harness(async () => pending.promise);
  const request = hidden.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-1',
  });
  hidden.setVisible({ conversationId: 'conv-2', tipNodeId: 'node-2' });
  pending.resolve(snapshot('conv-1', 'node-1'));
  await request;
  assert.deepEqual(hidden.snapshots, []);
}

async function testErrorIsVisibleButCancellationIsSilent() {
  const boom = new Error('transcript failed');
  const failed = harness(async () => {
    throw boom;
  });
  await failed.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-1',
  });
  assert.equal(failed.errors.at(-1), boom);

  const pending = deferred();
  const cancelled = harness(async () => pending.promise);
  const request = cancelled.coordinator.request({
    conversationId: 'conv-1',
    tipNodeId: 'node-1',
  });
  await Promise.resolve();
  cancelled.coordinator.cancelActive();
  pending.reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
  await request;
  assert.deepEqual(cancelled.errors, [null]);
  assert.deepEqual(cancelled.snapshots, []);
}

(async () => {
  await testSameVisibleTargetCoalescesOneRequest();
  await testNewTargetAbortsOldAndOnlyLatestCanCommit();
  await testMismatchedSnapshotAndHiddenTargetCannotCommit();
  await testErrorIsVisibleButCancellationIsSilent();
  console.log('transcriptRequestCoordinator tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

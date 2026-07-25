const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

require.extensions['.ts'] = function loadTs(module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: filename,
  }).outputText;
  module._compile(output, filename);
};

const {
  applyTranscriptPatch,
  normalizeTranscriptItems,
  stateFromTranscriptSnapshot,
} = require(path.join(__dirname, '../src/utils/transcriptItems.ts'));

function testNormalizeKeepsBackendTypedItemsInOrder() {
  const items = normalizeTranscriptItems([
    { id: 'u', type: 'user_message' },
    { id: 'p', type: 'plan_question' },
    { id: 'a', type: 'assistant_answer' },
  ]);

  assert.deepEqual(items.map((item) => `${item.type}:${item.id}`), [
    'user_message:u',
    'plan_question:p',
    'assistant_answer:a',
  ]);
}

function testSnapshotCreatesPatchState() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 5,
    items: [{ id: 'u', type: 'user_message' }],
  });

  assert.equal(state.conversationId, 'conv-1');
  assert.equal(state.nodeId, 'node-1');
  assert.equal(state.revision, 5);
  assert.deepEqual(state.items.map((item) => item.id), ['u']);
}

function testUpsertReplacesSameIdWithoutReplacingWholeList() {
  const first = { id: 'u', type: 'user_message', content: 'hello' };
  const last = { id: 'tail', type: 'assistant_answer', content: 'tail' };
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [first, { id: 'a', type: 'assistant_answer', content: 'old' }, last],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [{ op: 'upsert', item: { id: 'a', type: 'assistant_answer', content: 'new' }, index: 1 }],
  });

  assert.equal(result.status, 'applied');
  assert.equal(result.state.items[0], first);
  assert.equal(result.state.items[2], last);
  assert.deepEqual(result.state.items.map((item) => item.content), ['hello', 'new', 'tail']);
}

function testMultiUpsertReordersOnlyThePatchedSlice() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [
      { id: 'u', type: 'user_message' },
      { id: 'plan', type: 'plan_approval' },
      { id: 'process', type: 'assistant_process' },
    ],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [
      { op: 'upsert', item: { id: 'process', type: 'assistant_process' }, index: 1 },
      { op: 'upsert', item: { id: 'plan', type: 'plan_approval' }, index: 2 },
      { op: 'upsert', item: { id: 'answer', type: 'assistant_answer' }, index: 3 },
    ],
  });

  assert.equal(result.status, 'applied');
  assert.deepEqual(result.state.items.map((item) => item.id), ['u', 'process', 'plan', 'answer']);
}

function testRemoveDeletesByIdOnly() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [
      { id: 'keep', type: 'user_message' },
      { id: 'remove', type: 'assistant_process' },
    ],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [{ op: 'remove', id: 'remove' }],
  });

  assert.deepEqual(result.state.items.map((item) => item.id), ['keep']);
}

function testRevisionGapRequestsSnapshotCalibration() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 3,
    operations: [{ op: 'upsert', item: { id: 'a', type: 'assistant_answer', content: 'latest' }, index: 0 }],
  });

  assert.equal(result.status, 'snapshot_needed');
  assert.equal(result.state, state);
  assert.deepEqual(result.state.items.map((item) => item.id), []);
  assert.equal(result.state.revision, 1);
}

function testInitialTargetMismatchDiscardsPatchAndNeedsSnapshot() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'parent-node',
    revision: 0,
    items: [{ id: 'message:parent', type: 'user_message', content: 'parent' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'child-node',
    revision: 1,
    operations: [
      { op: 'upsert', item: { id: 'message:child-user', type: 'user_message', content: 'child' }, index: 0 },
      { op: 'upsert', item: { id: 'message:child-answer', type: 'assistant_answer', content: 'answer' }, index: 1 },
    ],
  });

  assert.equal(result.status, 'snapshot_needed');
  assert.equal(result.state, state);
  assert.deepEqual(result.state.items.map((item) => item.id), ['message:parent']);
}

function testDuplicateRevisionIsIgnored() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    items: [{ id: 'a', type: 'assistant_answer', content: 'current' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [{ op: 'upsert', item: { id: 'a', type: 'assistant_answer', content: 'stale' }, index: 0 }],
  });

  assert.equal(result.status, 'ignored');
  assert.equal(result.state, state);
}

function testNewUpsertUsesBackendIndexInsteadOfAppending() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [
      { id: 'u', type: 'user_message' },
      { id: 'answer', type: 'assistant_answer' },
    ],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [
      { op: 'upsert', item: { id: 'process', type: 'assistant_process' }, index: 1 },
    ],
  });

  assert.equal(result.status, 'applied');
  assert.deepEqual(result.state.items.map((item) => item.id), ['u', 'process', 'answer']);
}

function testUpsertWithoutBackendIndexNeedsSnapshot() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [{ id: 'u', type: 'user_message' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [{ op: 'upsert', item: { id: 'a', type: 'assistant_answer' } }],
  });

  assert.equal(result.status, 'snapshot_needed');
  assert.equal(result.state, state);
}

function testOldTypesAndLiveBuildersAreDeleted() {
  const transcriptTypes = fs.readFileSync(path.join(__dirname, '../src/types/transcript.ts'), 'utf8');
  const transcriptItems = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  for (const oldName of [
    ['tool', 'group'].join('_'),
    ['task', 'progress'].join('_'),
    ['run', 'draft'].join('_'),
    ['side', 'run', 'notification'].join('_'),
    ['compact', 'boundary'].join('_'),
    ['compact', 'summary'].join('_'),
    ['item', 'type'].join('_'),
  ]) {
    assert.doesNotMatch(transcriptTypes, new RegExp(oldName));
    assert.doesNotMatch(transcriptItems, new RegExp(oldName));
  }
}

testNormalizeKeepsBackendTypedItemsInOrder();
testSnapshotCreatesPatchState();
testUpsertReplacesSameIdWithoutReplacingWholeList();
testMultiUpsertReordersOnlyThePatchedSlice();
testRemoveDeletesByIdOnly();
testRevisionGapRequestsSnapshotCalibration();
testInitialTargetMismatchDiscardsPatchAndNeedsSnapshot();
testDuplicateRevisionIsIgnored();
testNewUpsertUsesBackendIndexInsteadOfAppending();
testUpsertWithoutBackendIndexNeedsSnapshot();
testOldTypesAndLiveBuildersAreDeleted();
console.log('transcriptItems tests passed');

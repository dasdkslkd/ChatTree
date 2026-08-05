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
  stateFromTranscriptSnapshot,
} = require(path.join(__dirname, '../src/utils/transcriptItems.ts'));

function testTargetMismatchNeedsSnapshot() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-2',
    revision: 2,
    operations: [],
  });

  assert.equal(result.status, 'snapshot_needed');
}

function testInitialTargetMismatchDiscardsPatchAndNeedsSnapshot() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 0,
    items: [{ id: 'message:old', type: 'user_message', content: 'old' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-2',
    revision: 1,
    operations: [{ op: 'upsert', item: { id: 'message:new', type: 'user_message', content: 'new' }, index: 0 }],
  });

  assert.equal(result.status, 'snapshot_needed');
  assert.equal(result.state, state);
  assert.deepEqual(result.state.items.map((item) => item.id), ['message:old']);
}

function testTargetMismatchDiscardsPatchAndNeedsSnapshot() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 4,
    items: [{ id: 'message:old', type: 'user_message', content: 'old' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-2',
    revision: 1,
    operations: [{ op: 'upsert', item: { id: 'message:new', type: 'user_message', content: 'new' }, index: 0 }],
  });

  assert.equal(result.status, 'snapshot_needed');
  assert.equal(result.state, state);
  assert.deepEqual(result.state.items.map((item) => item.id), ['message:old']);
}

function testAppendDeltaIsNotPartOfContract() {
  const source = fs.readFileSync(path.join(__dirname, '../src/types/transcript.ts'), 'utf8');
  assert.match(source, /op: 'upsert'/);
  assert.match(source, /op: 'remove'/);
  assert.doesNotMatch(source, /append|delta/);
}

function testPlanStateRefreshPathIsGone() {
  const manager = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const messageApi = fs.readFileSync(path.join(__dirname, '../src/api/message.ts'), 'utf8');
  const taskNotificationsApi = fs.readFileSync(path.join(__dirname, '../src/api/taskNotifications.ts'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  assert.match(manager, /type !== 'transcript_patch'/);
  assert.match(manager, /onTranscriptPatch/);
  assert.match(manager, /startPlanActionStream/);
  assert.match(messageApi, /answerPlanQuestion/);
  assert.match(messageApi, /approvePlan/);
  assert.match(messageApi, /rejectPlan/);
  assert.match(taskNotificationsApi, /transcript_patch/);
  assert.match(taskNotificationsApi, /bind:/);
  assert.match(taskNotificationsApi, /delete:/);
  assert.match(renderer, /case 'run_status':\s*return <RunStatusTranscriptItem item=\{item\} \/>;/);
  assert.doesNotMatch(renderer, /case 'run_status':\s*return <UnknownTranscriptItem/);
  assert.match(mainPage, /messageApi\.answerPlanQuestion/);
  assert.match(mainPage, /messageApi\.approvePlan/);
  assert.match(mainPage, /messageApi\.rejectPlan/);
  assert.match(mainPage, /onTranscriptPatch\(\(patch, sourceRun\)/);
  assert.match(mainPage, /shouldPatchRunIntoMainConversation\(sourceRun\)/);
  assert.match(mainPage, /operation\.item\.parent_node_id === visible\.tipNodeId/);
  assert.match(mainPage, /if \(!targetLandedFromVisibleNode\) return/);
  assert.match(mainPage, /transcriptState\.nodeId !== selectedBranchTipId[\s\S]{0,80}return new Set<string>\(\)/);
  assert.match(mainPage, /setCurrentNodeIdLocal\(patch\.node_id\)/);
  assert.match(mainPage, /loadTranscriptSnapshot\(patch\.conversation_id, patch\.node_id\)/);
  assert.match(
    mainPage,
    /await loadTranscriptSnapshot\(patch\.conversation_id, patch\.node_id\)[\s\S]{0,320}applyTranscriptPatch\(transcriptStateRef\.current, patch\)/,
  );
  assert.match(mainPage, /reason: 'plan-action-failed-calibration'[\s\S]{0,80}include: \['transcript'\]/);
  assert.doesNotMatch(mainPage, /patchTargetsVisibleRun/);
  assert.doesNotMatch(mainPage, new RegExp(['计划动作', ' stream 接口暂未接线'].join('')));
  assert.doesNotMatch(mainPage, /handlePlanActionNotConnected/);
  assert.doesNotMatch(mainPage, /services\/plans/);
  assert.doesNotMatch(manager, new RegExp(`${['onPlan', 'State'].join('')}|${['plan', 'state'].join('_')}`));
  assert.doesNotMatch(messageApi, new RegExp(`${['plan', 'state'].join('_')}`));
  assert.doesNotMatch(mainPage, new RegExp(`reason: '${['plan', 'state'].join('-')}'|${['stream', 'finished', 'transcript'].join('-')}`));
  assert.doesNotMatch(
    mainPage,
    new RegExp(`reason: '${['stream', 'finished'].join('-')}-[^']+'[\\s\\S]{0,180}include: \\[[^\\]]*'transcript'`),
  );
  assert.match(
    mainPage,
    /transcriptState\.conversationId === conversationId[\s\S]{0,120}transcriptState\.nodeId === selectedBranchTipId[\s\S]{0,80}return;/,
  );
}

function testEqualIndexUpsertsKeepOperationOrder() {
  const state = stateFromTranscriptSnapshot({
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 1,
    items: [{ id: 'message:root', type: 'user_message', content: 'root' }],
  });

  const result = applyTranscriptPatch(state, {
    type: 'transcript_patch',
    conversation_id: 'conv-1',
    node_id: 'node-1',
    revision: 2,
    operations: [
      { op: 'upsert', item: { id: 'message:continue', type: 'user_message', content: 'continue' }, index: 1 },
      { op: 'upsert', item: { id: 'process:node-1:0', type: 'assistant_process', blocks: [] }, index: 1 },
    ],
  });

  assert.equal(result.status, 'applied');
  assert.deepEqual(
    result.state.items.map((item) => item.id),
    ['message:root', 'message:continue', 'process:node-1:0'],
  );
}

testTargetMismatchNeedsSnapshot();
testInitialTargetMismatchDiscardsPatchAndNeedsSnapshot();
testTargetMismatchDiscardsPatchAndNeedsSnapshot();
testEqualIndexUpsertsKeepOperationOrder();
testAppendDeltaIsNotPartOfContract();
testPlanStateRefreshPathIsGone();
console.log('transcriptPatch tests passed');

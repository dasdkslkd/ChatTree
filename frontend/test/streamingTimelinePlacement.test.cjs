const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const transcriptItems = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
const streamManager = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');
const assistantAnswer = fs.readFileSync(
  path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'),
  'utf8',
);

function testMainTranscriptUsesSnapshotPlusPatchesOnly() {
  assert.match(mainPage, /items=\{displayTranscriptItems\}/);
  assert.match(mainPage, /const displayTranscriptItems = transcriptItems/);
  assert.match(mainPage, /streamManager\.onTranscriptPatch/);
}

function testPatchReducerHasOnlyUpsertRemovePlacement() {
  assert.match(transcriptItems, /operation\.op === 'remove'/);
  assert.match(transcriptItems, /Number\(operation\.index\)/);
  assert.match(transcriptItems, /upserts\.sort\(\(left, right\) => left\.index - right\.index\)/);
  assert.match(transcriptItems, /splice\(Math\.min\(index, nextItems\.length\), 0, item\)/);
  assert.doesNotMatch(transcriptItems, new RegExp(`overlay|anchorNodeId|${['run', 'draft'].join('_')}`));
}

function testStreamManagerConsumesTranscriptPatchPayloads() {
  assert.match(streamManager, /type !== 'transcript_patch'/);
  assert.match(streamManager, /applyTranscriptPatchChunk/);
  assert.match(
    streamManager,
    /item\.type === 'run_status'\)[\s\S]{0,80}\?\? renderedItems\.find\(\(item\) => item\.type === 'assistant_process'/,
  );
  assert.match(streamManager, /statusItem\.message\?\.trim\(\) \|\| state\.errorMessage/);
  assert.doesNotMatch(mainPage, /kind !== 'plan_action'/);
  assert.doesNotMatch(streamManager, /`plan_action_/);
  assert.doesNotMatch(streamManager, new RegExp(`${['plan', 'state'].join('_')}|${['onPlan', 'State'].join('')}`));
}

function testTranscriptErrorMessageWrapsInsteadOfTruncating() {
  const renderer = fs.readFileSync(
    path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'),
    'utf8',
  );
  assert.match(renderer, /whitespace-pre-wrap break-words/);
  assert.doesNotMatch(renderer, /truncate">\{item\.message \|\| label\}/);
}

function testIncompleteAnswerDisplaysProviderFinishReason() {
  assert.match(assistantAnswer, /item\.status === 'error' && item\.finish_reason/);
  assert.match(assistantAnswer, /生成未完成：\$\{item\.finish_reason\}/);
}

testMainTranscriptUsesSnapshotPlusPatchesOnly();
testPatchReducerHasOnlyUpsertRemovePlacement();
testStreamManagerConsumesTranscriptPatchPayloads();
testTranscriptErrorMessageWrapsInsteadOfTruncating();
testIncompleteAnswerDisplaysProviderFinishReason();
console.log('streamingTimelinePlacement tests passed');

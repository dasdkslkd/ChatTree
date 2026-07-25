const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const transcriptItems = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
const streamManager = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');

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
  assert.doesNotMatch(streamManager, new RegExp(`${['plan', 'state'].join('_')}|${['onPlan', 'State'].join('')}`));
}

testMainTranscriptUsesSnapshotPlusPatchesOnly();
testPatchReducerHasOnlyUpsertRemovePlacement();
testStreamManagerConsumesTranscriptPatchPayloads();
console.log('streamingTimelinePlacement tests passed');

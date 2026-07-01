const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testStreamingDraftsRenderChronologicalVisibleBlocks() {
  const visibleBlockUses = source.match(/draft\.streamingFoldState\.visibleBlocks/g) || [];
  assert.ok(visibleBlockUses.length >= 2, 'main and side streaming drafts should render visibleBlocks chronologically');
}

function testStreamingDraftsDoNotRenderAnswerDivider() {
  const streamingDraftSections = source.split('draft.streamingFoldState.canFoldProcess');
  assert.ok(streamingDraftSections.length >= 3, 'expected main and side streaming fold branches');
  for (const section of streamingDraftSections.slice(1, 3)) {
    const branch = section.split(') : (')[0];
    assert.equal(branch.includes('processed-answer-divider'), false);
    assert.equal(branch.includes('streamingFoldState.contentBlocks.map'), false);
    assert.equal(branch.includes('streamingFoldState.processBlocks'), false);
  }
}

testStreamingDraftsRenderChronologicalVisibleBlocks();
testStreamingDraftsDoNotRenderAnswerDivider();

console.log('streamingTimelinePlacement tests passed');

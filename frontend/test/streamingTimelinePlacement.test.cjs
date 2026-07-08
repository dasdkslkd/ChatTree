const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testStreamingDraftsRenderChronologicalVisibleBlocks() {
  const visibleBlockUses = source.match(/draft\.streamingFoldState\.visibleBlocks/g) || [];
  assert.ok(visibleBlockUses.length >= 1, 'side streaming drafts should render visibleBlocks chronologically');
}

function testStreamingDraftsDoNotRenderAnswerDivider() {
  const streamingDraftSections = source.split('draft.streamingFoldState.canFoldProcess');
  assert.ok(streamingDraftSections.length >= 2, 'expected side streaming fold branch');
  for (const section of streamingDraftSections.slice(1)) {
    const branch = section.split(') : (')[0];
    assert.equal(branch.includes('processed-answer-divider'), false);
    assert.equal(branch.includes('streamingFoldState.contentBlocks.map'), false);
    assert.equal(branch.includes('streamingFoldState.processBlocks'), false);
  }
}

function testMainTranscriptDoesNotRenderLocalStreamingDrafts() {
  assert.match(source, /<TranscriptList[\s\S]*items=\{displayTranscriptItems\}/);
  assert.match(source, /mergeLiveRunTranscriptItems/);
  assert.match(source, /renderItem=\{renderTranscriptItem\}/);
  assert.match(source, /createLiveAssistantTranscriptItems/);
  assert.doesNotMatch(source, /renderLiveRunDraftTranscriptItem/);
  const chatHistory = source.slice(source.indexOf('{/* Chat view */'), source.indexOf('{/* Tree view */'));
  assert.doesNotMatch(chatHistory, /draft\.streamingFoldState/);
  assert.doesNotMatch(chatHistory, /activeRunStates\.map\(/);
}

function testLiveImplementationOverlayDoesNotDependOnPlanProposalBlocks() {
  const transcriptItemsSource = fs.readFileSync(path.join(__dirname, '../src/utils/transcriptItems.ts'), 'utf8');
  assert.doesNotMatch(transcriptItemsSource, /block\.type === 'plan_proposal'/);
  assert.doesNotMatch(transcriptItemsSource, /block\.status === 'approved'/);
  assert.match(transcriptItemsSource, /overlay\.anchorNodeId/);
}

testStreamingDraftsRenderChronologicalVisibleBlocks();
testStreamingDraftsDoNotRenderAnswerDivider();
testMainTranscriptDoesNotRenderLocalStreamingDrafts();
testLiveImplementationOverlayDoesNotDependOnPlanProposalBlocks();

console.log('streamingTimelinePlacement tests passed');

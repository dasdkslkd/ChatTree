const assert = require('assert');
const fs = require('fs');
const path = require('path');

const assistantTimelinePath = path.join(__dirname, '../src/utils/assistantTimeline.ts');
const mainPagePath = path.join(__dirname, '../src/pages/MainPage.tsx');
const processItemPath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx');
const processTimelinePath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx');

function read(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function testAssistantTimelineModuleExistsAndExportsNormalizers() {
  const source = read(assistantTimelinePath);
  assert.match(source, /export type AssistantTimelineBlock/);
  assert.match(source, /export function normalizeLiveAssistantTimeline/);
  assert.match(source, /export function normalizePersistedAssistantTimeline/);
  assert.match(source, /export function normalizeLegacyToolInteractions/);
  assert.match(source, /export function createLiveAssistantProcessItem/);
  assert.match(source, /export function createLiveAssistantTranscriptItems/);
}

function testNoPlanProposalNormalization() {
  const source = read(assistantTimelinePath);
  assert.doesNotMatch(source, /type:\s*'plan_proposal'/);
  assert.doesNotMatch(source, /PlanProposalBlock/);
  assert.doesNotMatch(source, /normalizePlanProposal/);
}

function testMainPageDelegatesLiveRenderingToSharedProcessPath() {
  const source = read(mainPagePath);
  assert.match(source, /createLiveAssistantTranscriptItems/);
  assert.doesNotMatch(source, /function renderLiveRunDraftTranscriptItem/);
  assert.doesNotMatch(source, /const renderLiveRunDraftTranscriptItem\s*=/);
}

function testLiveAssistantTranscriptSplitsProcessAndAnswer() {
  const source = read(assistantTimelinePath);
  assert.match(source, /createLiveAssistantTranscriptItems\(run: StreamState\)/);
  assert.match(source, /createLiveAssistantProcessItem\(run,\s*\{\s*splitAnswer:\s*hasAnswer/);
  assert.match(source, /type:\s*'assistant_answer'/);
  assert.match(source, /stream_status:\s*run\.status/);
}

function testAssistantProcessItemIsThinAdapter() {
  const source = read(processItemPath);
  assert.match(source, /AssistantProcessTimeline/);
  assert.match(source, /allowProcessOnly:\s*true/);
  assert.doesNotMatch(source, /function ToolCallCard/);
  assert.doesNotMatch(source, /function getProcessTimeline/);
}

function testSharedTimelineRendererOwnsLiveStyle() {
  const source = read(processTimelinePath);
  assert.match(source, /cn\('processed-fold', processExpanded && 'expanded'\)/);
  assert.match(source, /setProcessExpanded/);
  assert.match(source, /aria-expanded=\{processExpanded\}/);
  assert.match(source, /!props\.streamingFoldState\?\.canFoldProcess && timeline\.length === 0/);
  assert.match(source, /props\.showStatusLabel === false/);
  assert.doesNotMatch(source, /PlanProposalCard/);
  assert.match(source, /ToolCallGroup/);
  assert.match(source, /getStreamStatusLabel/);
}

function testLiveRunsUseProcessedShellBeforeTimelineArrives() {
  const source = read(assistantTimelinePath);
  const mainPage = read(mainPagePath);
  assert.match(source, /getStreamingTimelineFoldState\([\s\S]*\{ allowProcessOnly: true \}/);
  assert.match(mainPage, /getStreamingTimelineFoldState\([\s\S]*\{ allowProcessOnly: true \}/);
  assert.match(mainPage, /!draft\.streamingFoldState\.canFoldProcess && draft\.timeline\.length === 0/);
}

testAssistantTimelineModuleExistsAndExportsNormalizers();
testNoPlanProposalNormalization();
testMainPageDelegatesLiveRenderingToSharedProcessPath();
testLiveAssistantTranscriptSplitsProcessAndAnswer();
testAssistantProcessItemIsThinAdapter();
testSharedTimelineRendererOwnsLiveStyle();
testLiveRunsUseProcessedShellBeforeTimelineArrives();
console.log('assistantTimeline tests passed');

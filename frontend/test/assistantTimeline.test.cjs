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
}

function testNoPlanProposalNormalization() {
  const source = read(assistantTimelinePath);
  assert.doesNotMatch(source, /type:\s*'plan_proposal'/);
  assert.doesNotMatch(source, /PlanProposalBlock/);
  assert.doesNotMatch(source, /normalizePlanProposal/);
}

function testMainPageDelegatesLiveRenderingToSharedProcessPath() {
  const source = read(mainPagePath);
  assert.match(source, /createLiveAssistantProcessItem/);
  assert.doesNotMatch(source, /function renderLiveRunDraftTranscriptItem/);
  assert.doesNotMatch(source, /const renderLiveRunDraftTranscriptItem\s*=/);
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
  assert.doesNotMatch(source, /PlanProposalCard/);
  assert.match(source, /ToolCallGroup/);
  assert.match(source, /getStreamStatusLabel/);
}

testAssistantTimelineModuleExistsAndExportsNormalizers();
testNoPlanProposalNormalization();
testMainPageDelegatesLiveRenderingToSharedProcessPath();
testAssistantProcessItemIsThinAdapter();
testSharedTimelineRendererOwnsLiveStyle();
console.log('assistantTimeline tests passed');

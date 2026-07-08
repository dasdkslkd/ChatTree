const assert = require('assert');
const fs = require('fs');
const path = require('path');
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

const assistantTimelinePath = path.join(__dirname, '../src/utils/assistantTimeline.ts');
const mainPagePath = path.join(__dirname, '../src/pages/MainPage.tsx');
const processItemPath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx');
const processTimelinePath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx');

function read(filePath) {
  return fs.readFileSync(filePath, 'utf8');
}

function testPersistedTimelineAggregatesAdjacentToolCalls() {
  const { normalizePersistedAssistantTimeline } = require(assistantTimelinePath);
  const blocks = normalizePersistedAssistantTimeline([
    { type: 'reasoning', key: 'reasoning-1', reasoning: 'thinking' },
    {
      type: 'tool_call',
      key: 'call-1',
      tool_call: { id: 'call-1', function: { name: 'read_file', arguments: '{"path":"a"}' } },
      tool_result: { tool_call_id: 'call-1', content: 'a' },
    },
    {
      type: 'tool_call',
      key: 'call-2',
      tool_call: { id: 'call-2', function: { name: 'write_file', arguments: '{"path":"b"}' } },
      tool_result: { tool_call_id: 'call-2', content: 'b' },
    },
    { type: 'content', key: 'content-1', content: 'done' },
  ]);

  assert.deepEqual(blocks.map((block) => block.type), ['reasoning', 'tools', 'content']);
  assert.equal(blocks[1].items.length, 2);
  assert.deepEqual(blocks[1].items.map((item) => item.name), ['read_file', 'write_file']);
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
testPersistedTimelineAggregatesAdjacentToolCalls();
testNoPlanProposalNormalization();
testMainPageDelegatesLiveRenderingToSharedProcessPath();
testLiveAssistantTranscriptSplitsProcessAndAnswer();
testAssistantProcessItemIsThinAdapter();
testSharedTimelineRendererOwnsLiveStyle();
testLiveRunsUseProcessedShellBeforeTimelineArrives();
console.log('assistantTimeline tests passed');

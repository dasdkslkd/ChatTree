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

const { normalizeTranscriptItems } = require(path.join(__dirname, '../src/utils/transcriptItems.ts'));

function testNormalizeKeepsBackendOrderAndFiltersHidden() {
  const items = normalizeTranscriptItems([
    { id: 'a', type: 'user_message', visibility: 'main' },
    { id: 'b', type: 'plan_card', visibility: 'hidden' },
    { id: 'c', type: 'run_draft', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['a', 'c']);
}

function testNormalizeOnlyKeepsMainVisibilityInOrder() {
  const items = normalizeTranscriptItems([
    { id: 'a', type: 'user_message' },
    { id: 'b', type: 'plan_card', visibility: 'side_panel' },
    { id: 'c', type: 'run_draft', visibility: 'main' },
    { id: 'd', type: 'tool_call', visibility: 'drawer' },
    { id: 'e', type: 'assistant_message', visibility: 'main' },
  ]);
  assert.deepEqual(items.map((item) => item.id), ['a', 'c', 'e']);
}

function testMainPageDelegatesTranscriptOrderingToTranscriptList() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(source, /<TranscriptList/);
  assert.doesNotMatch(source, /renderTaskLedgerStrip\(\)/);
  assert.doesNotMatch(source, /renderPlanApprovalCard\(\)/);
  assert.doesNotMatch(source, /activeRunDrafts\.map\(/);
}

function testPlanActionsAreRealCallbacks() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');

  assert.match(mainPage, /<TranscriptList[\s\S]*onApprovePlan=\{handleApprovePlan\}/);
  assert.match(mainPage, /<TranscriptList[\s\S]*onRejectPlan=\{handleRejectPlan\}/);
  assert.doesNotMatch(mainPage, /data-plan-actions/);
  assert.match(renderer, /onApprovePlan/);
  assert.match(renderer, /onRejectPlan/);
  assert.match(planCard, /onApprovePlan/);
  assert.match(planCard, /onRejectPlan/);
  assert.match(planCard, /onClick=\{\(\) => onApprovePlan\?\.\(item\)\}/);
  assert.match(planCard, /onClick=\{\(\) => onRejectPlan\?\.\(item\)\}/);
}

function testTranscriptRefreshUsesPerConversationRequestGuardsAndVisibleErrors() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.doesNotMatch(source, /transcriptRequestSeqRef/);
  assert.match(source, /transcriptRequestTokensRef/);
  assert.match(source, /getTranscriptRequestKey/);
  assert.match(source, /setTranscriptError/);
  assert.match(source, /transcriptError=\{transcriptError\}/);
}

function testTranscriptRefreshGuardsCurrentVisibleNodeBeforeWriting() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.match(source, /currentVisibleTranscriptKeyRef/);
  assert.match(source, /getTranscriptRequestKey\(currentConversation\.id,\s*selectedBranchTipId\)/);
  assert.match(source, /const isCurrentVisibleRequest = \(\) => requestKey === currentVisibleTranscriptKeyRef\.current/);
  assert.match(source, /if \(!isCurrentVisibleRequest\(\)\) return;\s*setTranscriptItems\(normalizeTranscriptItems\(items\)\)/);
}

function testPlanActionsUseTranscriptItemPlanIdInsteadOfActivePlanFallback() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.match(source, /const handleApprovePlan = useCallback\(async \(item: TranscriptItem\)/);
  assert.match(source, /const handleRejectPlan = useCallback\(async \(item: TranscriptItem\)/);
  assert.match(source, /const planId = item\.plan_id \|\| ''/);
  assert.match(source, /isTranscriptItemVisibleNow\(item,\s*currentConversation\?\.id \?\? null,\s*selectedBranchTipId\)/);
  assert.doesNotMatch(source, /const planId = activePlan\.plan_id \|\| activePlan\.id \|\| ''/);
}

function testTranscriptFallbackAndCopySurfacesAreVisible() {
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const list = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptList.tsx'), 'utf8');
  const userMessage = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/UserMessageItem.tsx'), 'utf8');
  const assistantAnswer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantAnswerItem.tsx'), 'utf8');
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.doesNotMatch(renderer, /default:\s*return null/);
  assert.match(renderer, /UnknownTranscriptItem/);
  assert.match(list, /transcript-empty/);
  assert.match(list, /transcript-error/);
  assert.match(userMessage, /onCopy/);
  assert.match(userMessage, /aria-label="复制消息"/);
  assert.match(assistantAnswer, /onCopy/);
  assert.match(assistantAnswer, /aria-label="复制消息"/);
  assert.match(mainPage, /onCopyItem=\{handleCopyTranscriptItem\}/);
}

testNormalizeKeepsBackendOrderAndFiltersHidden();
testNormalizeOnlyKeepsMainVisibilityInOrder();
testMainPageDelegatesTranscriptOrderingToTranscriptList();
testPlanActionsAreRealCallbacks();
testTranscriptRefreshUsesPerConversationRequestGuardsAndVisibleErrors();
testTranscriptRefreshGuardsCurrentVisibleNodeBeforeWriting();
testPlanActionsUseTranscriptItemPlanIdInsteadOfActivePlanFallback();
testTranscriptFallbackAndCopySurfacesAreVisible();
console.log('transcriptItems tests passed');

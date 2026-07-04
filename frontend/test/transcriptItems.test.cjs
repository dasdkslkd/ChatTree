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

function testNormalizeUsesBackendItemTypeWithoutReordering() {
  const items = normalizeTranscriptItems([
    { id: 'process-1', item_type: 'assistant_process', visibility: 'main' },
    { id: 'tool-1', item_type: 'tool_group', visibility: 'main' },
    { id: 'answer-1', item_type: 'assistant_answer', visibility: 'main' },
    { id: 'plan-1', item_type: 'plan_card', visibility: 'main' },
    { id: 'draft-1', item_type: 'run_draft', visibility: 'main' },
    { id: 'answer-2', item_type: 'assistant_answer', visibility: 'main' },
  ]);

  assert.deepEqual(items.map((item) => item.id), [
    'process-1',
    'tool-1',
    'answer-1',
    'plan-1',
    'draft-1',
    'answer-2',
  ]);
  assert.deepEqual(items.map((item) => item.type), [
    'assistant_process',
    'tool_group',
    'assistant_answer',
    'plan_card',
    'run_draft',
    'assistant_answer',
  ]);
}

function testNormalizeDoesNotGroupProcessToolAndAnswerBlocks() {
  const items = normalizeTranscriptItems([
    { id: 'p1', type: 'assistant_process', local_order: 30 },
    { id: 'a1', type: 'assistant_answer', local_order: 10 },
    { id: 't1', type: 'tool_group', local_order: 20 },
    { id: 'p2', type: 'assistant_process', local_order: 40 },
  ]);

  assert.deepEqual(items.map((item) => `${item.type}:${item.id}`), [
    'assistant_process:p1',
    'assistant_answer:a1',
    'tool_group:t1',
    'assistant_process:p2',
  ]);
}

function testMainPageDelegatesTranscriptOrderingToTranscriptList() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(source, /<TranscriptList/);
  assert.doesNotMatch(source, /renderTaskLedgerStrip\(\)/);
  assert.doesNotMatch(source, /renderPlanApprovalCard\(\)/);
  assert.doesNotMatch(source, /renderPlanQuestionCard/);
  assert.doesNotMatch(source, /activeRunDrafts\.map\(/);
}

function testMainPageKeepsAssistantTimelineOutOfMainTranscriptPath() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

  assert.doesNotMatch(source, /\bgetAssistantTimeline\b/);
  assert.match(source, /\bgetSideRunAssistantTimeline\b/);
  assert.match(source, /const sideRunDrafts = useMemo/);
  assert.doesNotMatch(source, /transcriptItems[\s\S]*activeRunStates[\s\S]*run_draft/);
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

function testPlanQuestionIsRenderedAndAnsweredFromTranscriptItem() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');

  assert.match(mainPage, /<TranscriptList[\s\S]*onAnswerPlanQuestion=\{handleAnswerPlanQuestion\}/);
  assert.doesNotMatch(mainPage, /\{renderPlanQuestionCard\(\)\}/);
  assert.match(renderer, /onAnswerPlanQuestion/);
  assert.match(planCard, /status === 'awaiting_question'/);
  assert.match(planCard, /onAnswerPlanQuestion\?\.\(item,\s*answer\)/);
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

function testPlanQuestionAnswerUsesTranscriptItemPlanIdInsteadOfActivePlanFallback() {
  const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = source.match(/const handleAnswerPlanQuestion = useCallback\(async \(item: TranscriptItem,\s*answerOverride\?: string\) => \{[\s\S]*?\n  \}, \[/);

  assert.ok(handlerMatch, 'handleAnswerPlanQuestion handler should accept the transcript item');
  assert.match(handlerMatch[0], /isTranscriptItemVisibleNow\(item,\s*currentConversation\?\.id \?\? null,\s*selectedBranchTipId\)/);
  assert.match(handlerMatch[0], /const planId = item\.plan_id \|\| ''/);
  assert.match(handlerMatch[0], /const actionNodeId = getTranscriptItemNodeId\(item\) \|\| selectedBranchTipId/);
  assert.doesNotMatch(handlerMatch[0], /activePlan/);
  assert.doesNotMatch(handlerMatch[0], /selectedBranchTipId,\s*\)/);
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

function testCopyHandlerOnlyReachesRealTranscriptMessages() {
  const renderer = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptItemRenderer.tsx'), 'utf8');
  const planCard = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');
  const runDraft = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/RunDraftItem.tsx'), 'utf8');
  const assistantProcess = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'), 'utf8');
  const toolGroup = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/ToolGroupItem.tsx'), 'utf8');

  assert.match(renderer, /case 'user_message':\s*return <UserMessageItem item=\{item\} onCopy=\{onCopyItem\} \/>/);
  assert.match(renderer, /case 'assistant_answer':\s*return <AssistantAnswerItem item=\{item\} onCopy=\{onCopyItem\} \/>/);
  const nonCopyableCases = renderer.match(/case 'assistant_process':[\s\S]*?case 'task_notification':/)?.[0] || '';
  assert.ok(nonCopyableCases, 'renderer should expose the non-copyable transcript cases');
  assert.doesNotMatch(nonCopyableCases, /\bonCopy=/);
  assert.doesNotMatch(nonCopyableCases, /\bonCopyItem\b/);
  assert.doesNotMatch(planCard, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(runDraft, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(assistantProcess, /aria-label="复制消息"|onCopy/);
  assert.doesNotMatch(toolGroup, /aria-label="复制消息"|onCopy/);
}

testNormalizeKeepsBackendOrderAndFiltersHidden();
testNormalizeOnlyKeepsMainVisibilityInOrder();
testNormalizeUsesBackendItemTypeWithoutReordering();
testNormalizeDoesNotGroupProcessToolAndAnswerBlocks();
testMainPageDelegatesTranscriptOrderingToTranscriptList();
testMainPageKeepsAssistantTimelineOutOfMainTranscriptPath();
testPlanActionsAreRealCallbacks();
testPlanQuestionIsRenderedAndAnsweredFromTranscriptItem();
testTranscriptRefreshUsesPerConversationRequestGuardsAndVisibleErrors();
testTranscriptRefreshGuardsCurrentVisibleNodeBeforeWriting();
testPlanActionsUseTranscriptItemPlanIdInsteadOfActivePlanFallback();
testPlanQuestionAnswerUsesTranscriptItemPlanIdInsteadOfActivePlanFallback();
testTranscriptFallbackAndCopySurfacesAreVisible();
testCopyHandlerOnlyReachesRealTranscriptMessages();
console.log('transcriptItems tests passed');

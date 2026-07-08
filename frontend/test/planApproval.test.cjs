const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
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

const {
  getPlanApprovalMarkdown,
  getPlanQuestionText,
  shouldShowPlanSummary,
  shouldShowPlanApproval,
  shouldShowPlanQuestion,
} = require(path.join(__dirname, '../src/utils/planApproval.ts'));

function testOnlyPendingApprovalWithMarkdownIsVisible() {
  assert.equal(shouldShowPlanApproval(null), false);
  assert.equal(shouldShowPlanApproval({ status: 'drafting', plan_markdown: '# Draft' }), false);
  assert.equal(shouldShowPlanApproval({ status: 'awaiting_approval', plan: '   ' }), false);
  assert.equal(shouldShowPlanApproval({ status: 'awaiting_approval', plan: '# Plan' }), true);
}

function testMarkdownFallsBackAcrossBackendFieldNames() {
  assert.equal(getPlanApprovalMarkdown({ plan: '# Primary', plan_markdown: '# Other' }), '# Primary');
  assert.equal(getPlanApprovalMarkdown({ plan_markdown: '# Primary', markdown: '# Other' }), '# Primary');
  assert.equal(getPlanApprovalMarkdown({ markdown: '# Secondary' }), '# Secondary');
  assert.equal(getPlanApprovalMarkdown({ content: '# Legacy' }), '# Legacy');
  assert.equal(getPlanApprovalMarkdown({ plan_markdown: '  # Trimmed  ' }), '# Trimmed');
}

function testQuestionOnlyVisibleWhenAwaitingQuestion() {
  assert.equal(shouldShowPlanQuestion(null), false);
  assert.equal(shouldShowPlanQuestion({ status: 'active', question: { question: '选择哪种布局？' } }), false);
  assert.equal(shouldShowPlanQuestion({ status: 'awaiting_question', question: { question: '   ' } }), false);
  assert.equal(shouldShowPlanQuestion({ status: 'awaiting_question', question: { question: '选择哪种布局？' } }), true);
  assert.equal(getPlanQuestionText({ question: { question: '  选择哪种布局？  ' } }), '选择哪种布局？');
}

function testApprovedPlanSummaryDisappears() {
  assert.equal(shouldShowPlanSummary(null), false);
  assert.equal(shouldShowPlanSummary({ status: 'awaiting_approval', plan: '# Plan' }), false);
  assert.equal(shouldShowPlanSummary({ status: 'approved', plan: '   ' }), false);
  assert.equal(shouldShowPlanSummary({ status: 'approved', plan: '# Plan' }), false);
}

function testPlanQuestionOptionClickOnlySelectsDraftAnswer() {
  const planCardSource = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanCardItem.tsx'), 'utf8');
  assert.match(planCardSource, /onClick=\{\(\) => setDraftAnswer\(label\)\}/);
  assert.doesNotMatch(planCardSource, /onClick=\{\(\) => onAnswerPlanQuestion\?\.\(item,\s*label\)\}/);
}

function testPlanProposalCardUsesTranscriptPlanCallbacks() {
  const proposalCardSource = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/PlanProposalCard.tsx'), 'utf8');
  const processSource = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'), 'utf8');
  const timelineSource = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx'), 'utf8');
  assert.match(proposalCardSource, /onApprove/);
  assert.match(proposalCardSource, /onReject/);
  assert.doesNotMatch(processSource, /onApprovePlan/);
  assert.doesNotMatch(processSource, /onRejectPlan/);
  assert.doesNotMatch(timelineSource, /plan_id: block\.plan_id/);
  assert.doesNotMatch(timelineSource, /PlanProposalCard/);
}

function testMainTranscriptUsesSharedLiveProcessRenderer() {
  const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(mainPage, /createLiveAssistantTranscriptItems/);
  assert.doesNotMatch(mainPage, /renderLiveRunDraftTranscriptItem/);
  assert.doesNotMatch(mainPage, /getLiveRunDraftTranscriptProps/);
}

function testApprovePlanStartsStructuredControlStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = mainPageSource.match(/const handleApprovePlan = useCallback\(async \(item: TranscriptItem\) => \{[\s\S]*?\n  \}, \[/);
  assert.ok(handlerMatch, 'handleApprovePlan handler should be present');
  assert.doesNotMatch(handlerMatch[0], /plansService\.approve/);
  assert.doesNotMatch(handlerMatch[0], /继续实现已批准的计划/);
  assert.doesNotMatch(handlerMatch[0], /void startStreaming\(/);
  assert.doesNotMatch(handlerMatch[0], /void streamManager\.startPlanApprovalStream\(/);
  assert.match(handlerMatch[0], /await streamManager\.startPlanApprovalStream\(/);
  assert.match(handlerMatch[0], /selectedBranchTipId/);
}

function testAnswerPlanQuestionStartsStructuredControlStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = mainPageSource.match(/const handleAnswerPlanQuestion = useCallback\(async \(item: TranscriptItem,\s*answerOverride\?: string\) => \{[\s\S]*?\n  \}, \[/);
  assert.ok(handlerMatch, 'handleAnswerPlanQuestion handler should be present');
  assert.doesNotMatch(handlerMatch[0], /void startStreaming\(/);
  assert.match(handlerMatch[0], /void streamManager\.startPlanAnswerStream\(/);
  assert.match(handlerMatch[0], /answer,/);
  assert.match(handlerMatch[0], /planId,/);
  assert.match(handlerMatch[0], /actionNodeId,\s*\)/);
}

function testRejectPlanStartsStructuredControlStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = mainPageSource.match(/const handleRejectPlan = useCallback\(async \(item: TranscriptItem\) => \{[\s\S]*?\n  \}, \[/);
  assert.ok(handlerMatch, 'handleRejectPlan handler should be present');
  assert.doesNotMatch(handlerMatch[0], /plansService\.reject\(/);
  assert.doesNotMatch(handlerMatch[0], /void streamManager\.startPlanRejectStream\(/);
  assert.match(handlerMatch[0], /await streamManager\.startPlanRejectStream\(/);
  assert.match(handlerMatch[0], /feedback,/);
  assert.match(handlerMatch[0], /planId,/);
  assert.match(handlerMatch[0], /actionNodeId,\s*\)/);
}

function testPlanRejectStreamEndpointIsWired() {
  const apiSource = fs.readFileSync(path.join(__dirname, '../src/api/message.ts'), 'utf8');
  const streamManagerSource = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');
  assert.match(apiSource, /streamPlanReject/);
  assert.match(apiSource, /\/reject\/stream/);
  assert.match(streamManagerSource, /startPlanRejectStream/);
  assert.match(streamManagerSource, /streamPlanReject/);
}

function testChatInputDoesNotExposePlanAsManualPermissionMode() {
  const chatInputSource = fs.readFileSync(path.join(__dirname, '../src/components/ChatInput.tsx'), 'utf8');
  assert.doesNotMatch(chatInputSource, /<DropdownMenuRadioItem\s+value=["']plan["']/);
}

function testActivePlanRefreshesWhenPlanToolEventsStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(mainPageSource, /activePlanToolSignal/);
  assert.match(mainPageSource, /enter_plan_mode/);
  assert.match(mainPageSource, /exit_plan_mode/);
  assert.match(mainPageSource, /refreshActivePlan\(conversationId\)/);
}

function main() {
  testOnlyPendingApprovalWithMarkdownIsVisible();
  testMarkdownFallsBackAcrossBackendFieldNames();
  testQuestionOnlyVisibleWhenAwaitingQuestion();
  testApprovedPlanSummaryDisappears();
  testPlanQuestionOptionClickOnlySelectsDraftAnswer();
  testPlanProposalCardUsesTranscriptPlanCallbacks();
  testMainTranscriptUsesSharedLiveProcessRenderer();
  testApprovePlanStartsStructuredControlStream();
  testAnswerPlanQuestionStartsStructuredControlStream();
  testRejectPlanStartsStructuredControlStream();
  testPlanRejectStreamEndpointIsWired();
  testChatInputDoesNotExposePlanAsManualPermissionMode();
  testActivePlanRefreshesWhenPlanToolEventsStream();
  console.log('planApproval tests passed');
}

main();

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

function testApprovedPlanSummaryRemainsVisible() {
  assert.equal(shouldShowPlanSummary(null), false);
  assert.equal(shouldShowPlanSummary({ status: 'awaiting_approval', plan: '# Plan' }), false);
  assert.equal(shouldShowPlanSummary({ status: 'approved', plan: '   ' }), false);
  assert.equal(shouldShowPlanSummary({ status: 'approved', plan: '# Plan' }), true);
}

function testMainPageOptionClickOnlySelectsDraftAnswer() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  assert.match(mainPageSource, /onClick=\{\(\) => setPlanQuestionAnswer\(label\)\}/);
  assert.doesNotMatch(mainPageSource, /onClick=\{\(\) => handleAnswerPlanQuestion\(label\)\}/);
}

function testApprovePlanStartsStructuredControlStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = mainPageSource.match(/const handleApprovePlan = useCallback\(async \(\) => \{[\s\S]*?\n  \}, \[/);
  assert.ok(handlerMatch, 'handleApprovePlan handler should be present');
  assert.doesNotMatch(handlerMatch[0], /plansService\.approve/);
  assert.doesNotMatch(handlerMatch[0], /继续实现已批准的计划/);
  assert.doesNotMatch(handlerMatch[0], /void startStreaming\(/);
  assert.match(handlerMatch[0], /void streamManager\.startPlanApprovalStream\(/);
  assert.match(handlerMatch[0], /selectedBranchTipId/);
}

function testAnswerPlanQuestionStartsStructuredControlStream() {
  const mainPageSource = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
  const handlerMatch = mainPageSource.match(/const handleAnswerPlanQuestion = useCallback\(async \(answerOverride\?: string\) => \{[\s\S]*?\n  \}, \[/);
  assert.ok(handlerMatch, 'handleAnswerPlanQuestion handler should be present');
  assert.doesNotMatch(handlerMatch[0], /void startStreaming\(/);
  assert.match(handlerMatch[0], /void streamManager\.startPlanAnswerStream\(/);
  assert.match(handlerMatch[0], /answer,/);
}

function testChatInputDoesNotExposePlanAsManualPermissionMode() {
  const chatInputSource = fs.readFileSync(path.join(__dirname, '../src/components/ChatInput.tsx'), 'utf8');
  assert.doesNotMatch(chatInputSource, /<DropdownMenuRadioItem\s+value=["']plan["']/);
}

function main() {
  testOnlyPendingApprovalWithMarkdownIsVisible();
  testMarkdownFallsBackAcrossBackendFieldNames();
  testQuestionOnlyVisibleWhenAwaitingQuestion();
  testApprovedPlanSummaryRemainsVisible();
  testMainPageOptionClickOnlySelectsDraftAnswer();
  testApprovePlanStartsStructuredControlStream();
  testAnswerPlanQuestionStartsStructuredControlStream();
  testChatInputDoesNotExposePlanAsManualPermissionMode();
  console.log('planApproval tests passed');
}

main();

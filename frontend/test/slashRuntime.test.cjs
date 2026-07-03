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
  applySlashCommandCompletion,
  getSlashCompletionCandidates,
  getSlashCompletionState,
  getSlashRunLabel,
  shouldQueueForMainThread,
  shouldRenderRunDraft,
} = require(path.join(__dirname, '../src/utils/slashRuntime.ts'));

function command(overrides = {}) {
  return {
    name: 'review',
    aliases: [],
    description: 'review current changes',
    supports_inline_args: true,
    requires_args: false,
    dispatch_kind: 'main_prompt',
    tool_policy: 'inherit',
    persistence_policy: 'main_thread',
    run_kind: 'chat',
    stream_target_policy: 'target_node',
    blocks_main_thread: true,
    enabled: true,
    ...overrides,
  };
}

function slashCommand(overrides = {}) {
  return {
    blocks_main_thread: true,
    ...overrides,
  };
}

function run(overrides = {}) {
  return {
    kind: 'chat',
    status: 'streaming',
    pendingUserMessage: null,
    content: '',
    reasoning: '',
    toolInteractions: [],
    pendingApprovals: {},
    ...overrides,
  };
}

function testNonBlockingSlashDoesNotQueueBehindMainChat() {
  assert.equal(
    shouldQueueForMainThread({
      currentBranchHasStreamingChat: true,
      slashCommand: slashCommand({ blocks_main_thread: false }),
    }),
    false,
  );
}

function testBlockingSlashStillQueuesBehindMainChat() {
  assert.equal(
    shouldQueueForMainThread({
      currentBranchHasStreamingChat: true,
      slashCommand: slashCommand({ blocks_main_thread: true }),
    }),
    true,
  );
}

function testPlainMessageQueuesBehindMainChat() {
  assert.equal(
    shouldQueueForMainThread({
      currentBranchHasStreamingChat: true,
      slashCommand: null,
    }),
    true,
  );
}

function testSlashCompletionActivatesOnlyAtFirstCharacter() {
  assert.deepEqual(getSlashCompletionState('/re'), { active: true, query: 're', args: '' });
  assert.equal(getSlashCompletionState(' /re').active, false);
  assert.equal(getSlashCompletionState('help /review').active, false);
}

function testSlashCompletionFiltersByNameAndAlias() {
  const candidates = getSlashCompletionCandidates('/rv', [
    command({ name: 'review', aliases: ['rv'] }),
    command({ name: 'init', aliases: [] }),
  ]);
  assert.deepEqual(candidates.map((item) => item.name), ['review']);
}

function testSlashCompletionLimitsCandidates() {
  const commands = Array.from({ length: 8 }, (_, index) => command({ name: `cmd${index}` }));
  assert.equal(getSlashCompletionCandidates('/', commands, 6).length, 6);
}

function testSlashCompletionStopsAfterCommandArgsBegin() {
  const commands = [command({ name: 'review' })];
  assert.deepEqual(getSlashCompletionCandidates('/review ', commands), []);
  assert.deepEqual(getSlashCompletionCandidates('/review focus on auth', commands), []);
}

function testSlashCompletionAppliesCanonicalCommandAndPreservesArgs() {
  assert.equal(
    applySlashCommandCompletion('/rv focus on auth', command({ name: 'review', aliases: ['rv'] })),
    '/review focus on auth',
  );
  assert.equal(applySlashCommandCompletion('/review', command({ name: 'review' })), '/review ');
}

function testForkWorkflowErrorRunsRenderAsDrafts() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'subagent', status: 'error' })), true);
  assert.equal(shouldRenderRunDraft(run({ kind: 'workflow', status: 'error' })), true);
}

function testForkWorkflowPendingSlashRunsRenderAsDrafts() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'subagent', pendingUserMessage: '/fork inspect' })), true);
  assert.equal(shouldRenderRunDraft(run({ kind: 'workflow', pendingUserMessage: '/workflow run' })), true);
}

function testRunningSubagentWithoutContentRendersAsDraft() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'subagent', status: 'streaming' })), true);
}

function testForkWorkflowPendingApprovalRunsRenderAsDrafts() {
  const pendingApprovals = {
    approval_1: { id: 'approval_1', status: 'pending', tool_name: 'shell_command' },
  };

  assert.equal(shouldRenderRunDraft(run({ kind: 'subagent', pendingApprovals })), true);
  assert.equal(shouldRenderRunDraft(run({ kind: 'workflow', pendingApprovals })), true);
}

function testBtwSideQuestionRunsRenderAsDrafts() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'side_question' })), true);
}

function testDirectResponseRunsRenderAsDrafts() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'direct_response', status: 'completed', content: 'status ok' })), true);
}

function testSidePanelRunLabelsUseSlashNames() {
  assert.equal(getSlashRunLabel('side_question'), 'btw');
  assert.equal(getSlashRunLabel('subagent'), 'fork');
  assert.equal(getSlashRunLabel('workflow'), 'workflow');
  assert.equal(getSlashRunLabel('direct_response'), 'status/help/capabilities');
}

function testUnknownBackgroundRunWithoutTranscriptStateDoesNotRenderAsDraft() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'unknown_background' })), false);
}

function testBackgroundWorkflowWithoutTranscriptStateDoesNotRenderAsDraft() {
  assert.equal(shouldRenderRunDraft(run({ kind: 'workflow' })), false);
}

function testWorkflowEventsRenderAsDraftState() {
  assert.equal(shouldRenderRunDraft(run({
    kind: 'workflow',
    workflowEvents: [{ eventType: 'phase_start', eventIndex: 1, phase: '检查' }],
  })), true);
}

function testObservedCompletedTerminalDoesNotRenderAsDraftState() {
  assert.equal(shouldRenderRunDraft(run({
    kind: 'terminal',
    status: 'completed',
    terminal: { stdout: 'short command output\n', stderr: '', events: [] },
    metadata: { terminal_notification_state: 'observed' },
  })), false);
}

function testObservedFailedTerminalDoesNotRenderAsDraftState() {
  assert.equal(shouldRenderRunDraft(run({
    kind: 'terminal',
    status: 'failed',
    terminal: { stdout: '', stderr: 'command failed\n', events: [] },
    metadata: { terminal_notification_state: 'observed' },
  })), false);
}

function testObservedCancelledTerminalDoesNotRenderAsDraftState() {
  assert.equal(shouldRenderRunDraft(run({
    kind: 'terminal',
    status: 'cancelled',
    terminal: { stdout: 'stopped\n', stderr: '', events: [] },
    metadata: { terminal_notification_state: 'observed' },
  })), false);
}

testNonBlockingSlashDoesNotQueueBehindMainChat();
testBlockingSlashStillQueuesBehindMainChat();
testPlainMessageQueuesBehindMainChat();
testSlashCompletionActivatesOnlyAtFirstCharacter();
testSlashCompletionFiltersByNameAndAlias();
testSlashCompletionLimitsCandidates();
testSlashCompletionStopsAfterCommandArgsBegin();
testSlashCompletionAppliesCanonicalCommandAndPreservesArgs();
testForkWorkflowErrorRunsRenderAsDrafts();
testForkWorkflowPendingSlashRunsRenderAsDrafts();
testRunningSubagentWithoutContentRendersAsDraft();
testForkWorkflowPendingApprovalRunsRenderAsDrafts();
testBtwSideQuestionRunsRenderAsDrafts();
testDirectResponseRunsRenderAsDrafts();
testSidePanelRunLabelsUseSlashNames();
testUnknownBackgroundRunWithoutTranscriptStateDoesNotRenderAsDraft();
testBackgroundWorkflowWithoutTranscriptStateDoesNotRenderAsDraft();
testWorkflowEventsRenderAsDraftState();
testObservedCompletedTerminalDoesNotRenderAsDraftState();
testObservedFailedTerminalDoesNotRenderAsDraftState();
testObservedCancelledTerminalDoesNotRenderAsDraftState();

console.log('slashRuntime tests passed');

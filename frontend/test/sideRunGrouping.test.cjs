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
  SIDE_RUN_GROUP_ORDER,
  groupDetachedSideRuns,
  getWorkflowStepOrder,
} = require(path.join(__dirname, '../src/utils/sideRunGrouping.ts'));

function run(overrides = {}) {
  return {
    run: {
      runId: overrides.runId || 'run-default',
      kind: overrides.kind || 'subagent',
      status: overrides.status || 'streaming',
      createdAt: overrides.createdAt ?? 1000,
      parentRunId: overrides.parentRunId ?? null,
      metadata: overrides.metadata ?? {},
    },
    timeline: overrides.timeline || [],
    showPendingBubble: false,
    showStreamBlock: true,
    streamingFoldState: { canFoldProcess: false, visibleBlocks: [] },
    activeReasoningIndex: -1,
    activeReasoningKey: null,
  };
}

function testTopLevelRunsAreGroupedInDefinitionOrderAndSortedNewestFirst() {
  const groups = groupDetachedSideRuns([
    run({ runId: 'chat-main', kind: 'chat', createdAt: 9000 }),
    run({ runId: 'workflow-step-a', kind: 'workflow_step', parentRunId: 'workflow-1', createdAt: 7000 }),
    run({ runId: 'workflow-old', kind: 'workflow', createdAt: 1000 }),
    run({ runId: 'fork-old', kind: 'subagent', createdAt: 2000 }),
    run({ runId: 'command-new', kind: 'command', createdAt: 6500 }),
    run({ runId: 'status-new', kind: 'direct_response', createdAt: 8000 }),
    run({ runId: 'btw-new', kind: 'side_question', createdAt: 6000 }),
    run({ runId: 'fork-new', kind: 'subagent', createdAt: 5000 }),
    run({ runId: 'workflow-new', kind: 'workflow', createdAt: 4000 }),
  ]);

  assert.deepEqual(SIDE_RUN_GROUP_ORDER, ['side_question', 'subagent', 'command', 'workflow', 'direct_response']);
  assert.deepEqual(groups.map((group) => group.kind), ['side_question', 'subagent', 'command', 'workflow', 'direct_response']);
  assert.deepEqual(groups[0].runs.map((draft) => draft.run.runId), ['btw-new']);
  assert.deepEqual(groups[1].runs.map((draft) => draft.run.runId), ['fork-new', 'fork-old']);
  assert.deepEqual(groups[2].runs.map((draft) => draft.run.runId), ['command-new']);
  assert.deepEqual(groups[3].runs.map((draft) => draft.run.runId), ['workflow-new', 'workflow-old']);
  assert.deepEqual(groups[4].runs.map((draft) => draft.run.runId), ['status-new']);
}

function testWorkflowStepsAttachToParentWorkflowAndUseDefinitionOrder() {
  const groups = groupDetachedSideRuns([
    run({ runId: 'workflow-1', kind: 'workflow', createdAt: 1000 }),
    run({ runId: 'step-third', kind: 'workflow_step', parentRunId: 'workflow-1', createdAt: 9000, metadata: { step_index: 2 } }),
    run({ runId: 'step-first', kind: 'workflow_step', parentRunId: 'workflow-1', createdAt: 8000, metadata: { order: 0 } }),
    run({ runId: 'step-second', kind: 'workflow_step', parentRunId: 'workflow-1', createdAt: 7000, metadata: { workflow_step_index: 1 } }),
  ]);

  const workflowGroup = groups.find((group) => group.kind === 'workflow');
  assert.ok(workflowGroup);
  const workflow = workflowGroup.runs[0];
  assert.deepEqual(workflow.steps.map((step) => step.run.runId), ['step-first', 'step-second', 'step-third']);
  assert.deepEqual(workflow.steps.map((step) => getWorkflowStepOrder(step.run)), [0, 1, 2]);
}

function testParentedSubagentRunsAttachToWorkflowInsteadOfTopLevelForks() {
  const groups = groupDetachedSideRuns([
    run({ runId: 'workflow-1', kind: 'workflow', createdAt: 1000 }),
    run({ runId: 'top-fork', kind: 'subagent', createdAt: 5000 }),
    run({ runId: 'workflow-child', kind: 'subagent', parentRunId: 'workflow-1', createdAt: 6000, metadata: { workflow_step_index: 0 } }),
  ]);

  const forkGroup = groups.find((group) => group.kind === 'subagent');
  assert.deepEqual(forkGroup.runs.map((item) => item.run.runId), ['top-fork']);

  const workflowGroup = groups.find((group) => group.kind === 'workflow');
  assert.deepEqual(workflowGroup.runs[0].steps.map((step) => step.run.runId), ['workflow-child']);
}

function testParentedSubagentFromMainRunStillAppearsAsTopLevelFork() {
  const groups = groupDetachedSideRuns([
    run({ runId: 'main-child-fork', kind: 'subagent', parentRunId: 'run-main-chat', createdAt: 6000 }),
    run({ runId: 'top-fork', kind: 'subagent', createdAt: 5000 }),
  ]);

  const forkGroup = groups.find((group) => group.kind === 'subagent');
  assert.ok(forkGroup);
  assert.deepEqual(forkGroup.runs.map((item) => item.run.runId), ['main-child-fork', 'top-fork']);
}

testTopLevelRunsAreGroupedInDefinitionOrderAndSortedNewestFirst();
testWorkflowStepsAttachToParentWorkflowAndUseDefinitionOrder();
testParentedSubagentRunsAttachToWorkflowInsteadOfTopLevelForks();
testParentedSubagentFromMainRunStillAppearsAsTopLevelFork();

console.log('sideRunGrouping tests passed');

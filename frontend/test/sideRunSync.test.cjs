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
  SIDE_RUN_KINDS,
  TERMINAL_RUN_STATUSES,
  getVisibleSideRunRecords,
  isTerminalRunStatus,
} = require(path.join(__dirname, '../src/utils/sideRunSync.ts'));

function run(overrides = {}) {
  return {
    run_id: overrides.run_id || 'run-default',
    conversation_id: 'conv-1',
    kind: overrides.kind || 'subagent',
    status: overrides.status || 'running',
    anchor_node_id: overrides.anchor_node_id ?? 'node-anchor',
    target_node_id: overrides.target_node_id ?? null,
    parent_run_id: overrides.parent_run_id ?? null,
    event_count: 1,
    created_at: 10,
    updated_at: 11,
    metadata: overrides.metadata || {},
  };
}

function testSideRunKindSetIncludesDetachedRunTypes() {
  assert.deepEqual([...SIDE_RUN_KINDS], ['side_question', 'subagent', 'terminal', 'workflow', 'workflow_step', 'direct_response']);
  assert.deepEqual([...TERMINAL_RUN_STATUSES], ['completed', 'failed', 'cancelled']);
}

function testVisibleSideRunsIncludeToolSpawnedParentedSubagents() {
  const visible = getVisibleSideRunRecords([
    run({ run_id: 'main-chat', kind: 'chat', parent_run_id: null }),
    run({ run_id: 'tool-spawned-subagent', kind: 'subagent', parent_run_id: 'run-main-chat' }),
    run({ run_id: 'workflow-child', kind: 'subagent', parent_run_id: 'run-workflow', metadata: { source_run_id: 'run-workflow' } }),
    run({ run_id: 'terminal-child', kind: 'terminal', parent_run_id: 'run-main-chat' }),
  ], new Set());

  assert.deepEqual(visible.map((item) => item.run_id), ['tool-spawned-subagent', 'workflow-child', 'terminal-child']);
}

function testVisibleSideRunsExcludeHiddenRuns() {
  const visible = getVisibleSideRunRecords([
    run({ run_id: 'visible-subagent' }),
    run({ run_id: 'hidden-subagent' }),
  ], new Set(['hidden-subagent']));

  assert.deepEqual(visible.map((item) => item.run_id), ['visible-subagent']);
}

function testTerminalStatusDetectionMatchesBackendRunStatuses() {
  assert.equal(isTerminalRunStatus('running'), false);
  assert.equal(isTerminalRunStatus('waiting_approval'), false);
  assert.equal(isTerminalRunStatus('completed'), true);
  assert.equal(isTerminalRunStatus('failed'), true);
  assert.equal(isTerminalRunStatus('cancelled'), true);
}

testSideRunKindSetIncludesDetachedRunTypes();
testVisibleSideRunsIncludeToolSpawnedParentedSubagents();
testVisibleSideRunsExcludeHiddenRuns();
testTerminalStatusDetectionMatchesBackendRunStatuses();

console.log('sideRunSync tests passed');

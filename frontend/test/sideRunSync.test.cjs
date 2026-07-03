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
  COMMAND_RUN_STATUSES,
  getVisibleSideRunRecords,
  isCommandRunStatus,
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
  assert.deepEqual([...SIDE_RUN_KINDS], ['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response']);
  assert.deepEqual([...COMMAND_RUN_STATUSES], ['completed', 'failed', 'cancelled']);
}

function testVisibleSideRunsIncludeToolSpawnedParentedSubagents() {
  const visible = getVisibleSideRunRecords([
    run({ run_id: 'main-chat', kind: 'chat', parent_run_id: null }),
    run({ run_id: 'tool-spawned-subagent', kind: 'subagent', parent_run_id: 'run-main-chat' }),
    run({ run_id: 'workflow-child', kind: 'subagent', parent_run_id: 'run-workflow', metadata: { source_run_id: 'run-workflow' } }),
    run({ run_id: 'command-child', kind: 'command', parent_run_id: 'run-main-chat' }),
  ], new Set());

  assert.deepEqual(visible.map((item) => item.run_id), ['tool-spawned-subagent', 'workflow-child', 'command-child']);
}

function testVisibleSideRunsExcludeHiddenRuns() {
  const visible = getVisibleSideRunRecords([
    run({ run_id: 'visible-subagent' }),
    run({ run_id: 'hidden-subagent' }),
  ], new Set(['hidden-subagent']));

  assert.deepEqual(visible.map((item) => item.run_id), ['visible-subagent']);
}

function testCommandStatusDetectionMatchesBackendRunStatuses() {
  assert.equal(isCommandRunStatus('running'), false);
  assert.equal(isCommandRunStatus('waiting_approval'), false);
  assert.equal(isCommandRunStatus('completed'), true);
  assert.equal(isCommandRunStatus('failed'), true);
  assert.equal(isCommandRunStatus('cancelled'), true);
}

testSideRunKindSetIncludesDetachedRunTypes();
testVisibleSideRunsIncludeToolSpawnedParentedSubagents();
testVisibleSideRunsExcludeHiddenRuns();
testCommandStatusDetectionMatchesBackendRunStatuses();

console.log('sideRunSync tests passed');

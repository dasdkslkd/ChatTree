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
  extractSideRunNotifications,
  collectSideRunNotifications,
} = require(path.join(__dirname, '../src/utils/sideRunNotifications.ts'));

function testExtractsSpawnAgentRunIdFromToolResultContent() {
  const notifications = extractSideRunNotifications([
    {
      tools: [
        {
          name: 'spawn_agent',
          content: JSON.stringify({
            run_id: 'run-subagent-1',
            kind: 'subagent',
            status: 'running',
          }),
        },
      ],
    },
  ]);

  assert.deepEqual(notifications, [{ runId: 'run-subagent-1', kind: 'subagent' }]);
}

function testExtractsCompatibilityAliasSubagentRunId() {
  const notifications = extractSideRunNotifications([
    {
      tools: [
        {
          name: 'start_subagent',
          content: JSON.stringify({
            run_id: 'run-subagent-2',
            kind: 'subagent',
            replacement_tool: 'spawn_agent',
          }),
        },
      ],
    },
  ]);

  assert.deepEqual(notifications, [{ runId: 'run-subagent-2', kind: 'subagent' }]);
}

function testIgnoresNonSideRunToolResults() {
  const notifications = extractSideRunNotifications([
    {
      tools: [
        { name: 'read_file', content: JSON.stringify({ path: 'README.md' }) },
        { name: 'run_command', content: 'plain text' },
      ],
    },
  ]);

  assert.deepEqual(notifications, []);
}

function testDeduplicatesRunNotifications() {
  const notifications = extractSideRunNotifications([
    {
      tools: [
        { name: 'spawn_agent', content: JSON.stringify({ run_id: 'run-same', kind: 'subagent' }) },
        { name: 'spawn_agent', raw_content: JSON.stringify({ run_id: 'run-same', kind: 'subagent' }) },
      ],
    },
  ]);

  assert.deepEqual(notifications, [{ runId: 'run-same', kind: 'subagent' }]);
}

function testCollectsToolAndStreamSideRunNotificationsForSharedDeduping() {
  const notifications = collectSideRunNotifications(
    [
      {
        tools: [
          { name: 'spawn_agent', content: JSON.stringify({ run_id: 'run-tool', kind: 'subagent' }) },
        ],
      },
    ],
    [
      { runId: 'run-child', kind: 'subagent' },
      { runId: 'run-tool', kind: 'subagent' },
    ],
  );

  assert.deepEqual(notifications, [
    { runId: 'run-tool', kind: 'subagent' },
    { runId: 'run-child', kind: 'subagent' },
  ]);
}

testExtractsSpawnAgentRunIdFromToolResultContent();
testExtractsCompatibilityAliasSubagentRunId();
testIgnoresNonSideRunToolResults();
testDeduplicatesRunNotifications();
testCollectsToolAndStreamSideRunNotificationsForSharedDeduping();

console.log('sideRunNotifications tests passed');

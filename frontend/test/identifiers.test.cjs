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

const identifiers = require(path.join(__dirname, '../src/utils/identifiers.ts'));

function testSideRunKindSetIncludesDetachedRunTypes() {
  assert.deepEqual(
    [...identifiers.SIDE_RUN_KINDS],
    ['side_question', 'subagent', 'command', 'workflow', 'workflow_step', 'direct_response'],
  );
}

function testSideRunKindDetectionSupportsLiveRunUiOnly() {
  assert.equal(identifiers.isSideRunKind('subagent'), true);
  assert.equal(identifiers.isSideRunKind('chat'), false);
}

function testHistoricalRunAttachCollectorIsRemoved() {
  assert.equal(identifiers.getVisibleSideRunRecords, undefined);
  assert.equal(identifiers.COMMAND_RUN_STATUSES, undefined);
}

testSideRunKindSetIncludesDetachedRunTypes();
testSideRunKindDetectionSupportsLiveRunUiOnly();
testHistoricalRunAttachCollectorIsRemoved();

console.log('identifiers tests passed');

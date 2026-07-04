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

testNormalizeKeepsBackendOrderAndFiltersHidden();
testNormalizeOnlyKeepsMainVisibilityInOrder();
console.log('transcriptItems tests passed');

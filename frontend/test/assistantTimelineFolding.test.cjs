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

const foldingPath = path.join(__dirname, '../src/utils/assistantTimelineFolding.ts');
const {
  formatProcessedDuration,
  getStreamingTimelineFoldState,
  getTimelineFoldState,
} = require(foldingPath);

function testFormatsProcessedDurationWithHours() {
  assert.equal(formatProcessedDuration(5_550_000), '1h32m30s');
  assert.equal(formatProcessedDuration(4_202_000), '1h10m2s');
  assert.equal(formatProcessedDuration(62_000), '1m2s');
  assert.equal(formatProcessedDuration(900), '1s');
  assert.equal(formatProcessedDuration(null), null);
}

function testCollapsesHistoricalProcessBlocks() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
    { type: 'content', key: 'c1' },
  ];

  const folded = getTimelineFoldState(blocks, {
    processExpanded: false,
  });

  assert.equal(folded.canFoldProcess, true);
  assert.deepEqual(folded.processBlocks.map((block) => block.key), ['r1', 't1']);
  assert.deepEqual(folded.contentBlocks.map((block) => block.key), ['c1']);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['c1']);
}

function testExplicitEmptyFinalKeysFoldAllProcessText() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'content', key: 'content-0' },
    { type: 'tools', key: 't1' },
  ];

  const folded = getTimelineFoldState(blocks, {
    processExpanded: false,
    finalContentKeys: [],
    allowProcessOnly: true,
  });

  assert.equal(folded.canFoldProcess, true);
  assert.deepEqual(folded.processBlocks.map((block) => block.key), ['r1', 'content-0', 't1']);
  assert.deepEqual(folded.contentBlocks.map((block) => block.key), []);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), []);
}

function testStreamingProcessDefaultsExpanded() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
    { type: 'content', key: 'content-final' },
  ];

  const folded = getStreamingTimelineFoldState(blocks, ['content-final']);

  assert.equal(folded.canFoldProcess, true);
  assert.equal(folded.processExpanded, true);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 't1', 'content-final']);
}

function testFoldingModuleDoesNotInspectAssistantMessages() {
  const source = fs.readFileSync(foldingPath, 'utf8');
  assert.doesNotMatch(source, new RegExp([
    ['tool', 'interactions'].join('_'),
    ['tool', 'calls'].join('_'),
    ['tool', 'results'].join('_'),
    'hasAssistantProcessHistory',
    'getAssistantFoldedContentBlocks',
  ].join('|')));
  assert.doesNotMatch(source, /stripChronologicalPrefix|interactionHasProcessHistory/);
}

testFormatsProcessedDurationWithHours();
testCollapsesHistoricalProcessBlocks();
testExplicitEmptyFinalKeysFoldAllProcessText();
testStreamingProcessDefaultsExpanded();
testFoldingModuleDoesNotInspectAssistantMessages();
console.log('assistantTimelineFolding tests passed');

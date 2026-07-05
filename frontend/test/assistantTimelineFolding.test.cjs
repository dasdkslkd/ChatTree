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
  formatProcessedDuration,
  getAssistantFoldedContentBlocks,
  getStreamingTimelineFoldState,
  getTimelineFoldState,
  hasAssistantProcessHistory,
} = require(path.join(__dirname, '../src/utils/assistantTimelineFolding.ts'));

function testFormatsProcessedDurationWithHours() {
  assert.equal(formatProcessedDuration(4_202_000), '1h 10m 2s');
  assert.equal(formatProcessedDuration(62_000), '1m 2s');
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

function testCollapsesProcessBlocksEvenForLatestCompletedAssistant() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
    { type: 'content', key: 'c1' },
  ];

  const folded = getTimelineFoldState(blocks, {
    processExpanded: false,
  });

  assert.equal(folded.canFoldProcess, true);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['c1']);
}

function testDoesNotFoldWhenThereIsNoContentBlock() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
  ];

  const folded = getTimelineFoldState(blocks, {
    processExpanded: false,
  });

  assert.equal(folded.canFoldProcess, false);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 't1']);
}

function testKeepsIntermediateContentInContentBlocksWhenFinalKeysProvided() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'content', key: 'content-0' },
    { type: 'tools', key: 't1' },
    { type: 'content', key: 'content-final' },
  ];

  const folded = getTimelineFoldState(blocks, {
    processExpanded: true,
    finalContentKeys: ['content-final'],
  });

  assert.equal(folded.canFoldProcess, true);
  assert.deepEqual(folded.processBlocks.map((block) => block.key), ['r1', 't1']);
  assert.deepEqual(folded.contentBlocks.map((block) => block.key), ['content-0', 'content-final']);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 'content-0', 't1', 'content-final']);
}

function testStreamingProcessDefaultsExpandedAndKeepsAnswerSeparated() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
    { type: 'content', key: 'content-final' },
  ];

  const folded = getStreamingTimelineFoldState(blocks, ['content-final']);

  assert.equal(folded.canFoldProcess, true);
  assert.equal(folded.processExpanded, true);
  assert.deepEqual(folded.processBlocks.map((block) => block.key), ['r1', 't1']);
  assert.deepEqual(folded.contentBlocks.map((block) => block.key), ['content-final']);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 't1', 'content-final']);
}

function testStreamingDoesNotInventFoldForProcessOnlyDrafts() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
  ];

  const folded = getStreamingTimelineFoldState(blocks);

  assert.equal(folded.canFoldProcess, false);
  assert.equal(folded.processExpanded, true);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 't1']);
}

function testCompletedProcessOnlyTimelineCanUseProcessedShell() {
  const blocks = [
    { type: 'reasoning', key: 'r1' },
    { type: 'tools', key: 't1' },
  ];

  const folded = getStreamingTimelineFoldState(blocks, [], { allowProcessOnly: true });

  assert.equal(folded.canFoldProcess, true);
  assert.equal(folded.processExpanded, true);
  assert.deepEqual(folded.visibleBlocks.map((block) => block.key), ['r1', 't1']);
}

function testDetectsAssistantProcessHistoryWithoutFormattingTools() {
  assert.equal(hasAssistantProcessHistory({
    tool_interactions: [{
      assistant: {
        tool_calls: [{ id: 'call-1', name: 'read_file' }],
      },
      tools: [{ tool_call_id: 'call-1', content: 'large raw output' }],
    }],
  }), true);

  assert.equal(hasAssistantProcessHistory({ content: 'final answer' }), false);
}

function testExtractsFoldedFinalContentWithoutToolBlocks() {
  const blocks = getAssistantFoldedContentBlocks({
    content: 'draft\nfinal answer',
    tool_interactions: [{
      assistant: { content: 'draft\n' },
      tools: [{ content: 'large raw output' }],
    }],
  });

  assert.deepEqual(blocks, [{ type: 'content', key: 'content-final', content: 'final answer' }]);
}

function main() {
  testFormatsProcessedDurationWithHours();
  testCollapsesHistoricalProcessBlocks();
  testCollapsesProcessBlocksEvenForLatestCompletedAssistant();
  testDoesNotFoldWhenThereIsNoContentBlock();
  testKeepsIntermediateContentInContentBlocksWhenFinalKeysProvided();
  testStreamingProcessDefaultsExpandedAndKeepsAnswerSeparated();
  testStreamingDoesNotInventFoldForProcessOnlyDrafts();
  testCompletedProcessOnlyTimelineCanUseProcessedShell();
  testDetectsAssistantProcessHistoryWithoutFormattingTools();
  testExtractsFoldedFinalContentWithoutToolBlocks();
  console.log('assistantTimelineFolding tests passed');
}

main();

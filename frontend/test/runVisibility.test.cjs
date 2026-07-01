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
  isDetachedRunView,
  isRunVisibleInMainTranscript,
  isRunBlockingSelectedBranch,
  isRunVisibleInSelectedTranscript,
} = require('../src/utils/runVisibility.ts');

function chatRun(overrides = {}) {
  return {
    kind: 'chat',
    status: 'streaming',
    anchorNodeId: null,
    nodeId: null,
    targetNodeId: null,
    ...overrides,
  };
}

function testChildRunIsHiddenFromParentTranscript() {
  const run = chatRun({
    anchorNodeId: 'node-hello',
    nodeId: 'node-openai',
    targetNodeId: 'node-openai',
  });
  const parentHistory = new Set(['node-hello']);

  assert.equal(isRunVisibleInSelectedTranscript(run, 'node-hello', parentHistory), false);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-hello', parentHistory), false);
}

function testChildRunIsVisibleOnItsOwnBranch() {
  const run = chatRun({
    anchorNodeId: 'node-hello',
    nodeId: 'node-openai',
    targetNodeId: 'node-openai',
  });

  assert.equal(isRunVisibleInSelectedTranscript(run, 'node-openai', new Set(['node-hello'])), true);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-openai', new Set(['node-hello'])), true);
}

function testExistingBranchRunIsVisibleWhenTargetIsInHistory() {
  const run = chatRun({
    anchorNodeId: 'node-hello',
    nodeId: 'node-openai',
    targetNodeId: 'node-openai',
  });

  assert.equal(isRunVisibleInSelectedTranscript(run, 'node-child', new Set(['node-hello', 'node-openai'])), true);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-child', new Set(['node-hello', 'node-openai'])), true);
}

function testPreTargetRunStillBelongsToSelectedAnchor() {
  const run = chatRun({
    anchorNodeId: 'node-hello',
    nodeId: 'node-hello',
    targetNodeId: 'node-hello',
  });

  assert.equal(isRunVisibleInSelectedTranscript(run, 'node-hello', new Set(['node-hello'])), true);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-hello', new Set(['node-hello'])), true);
}

function testNonStreamingRunDoesNotBlockBranch() {
  const run = chatRun({
    status: 'completed',
    anchorNodeId: 'node-hello',
    nodeId: 'node-openai',
    targetNodeId: 'node-openai',
  });

  assert.equal(isRunBlockingSelectedBranch(run, 'node-openai', new Set(['node-hello'])), false);
}

function testDetachedBackgroundRunIsSideViewNotMainTranscript() {
  const run = chatRun({
    kind: 'subagent',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello'), true);
  assert.equal(isRunVisibleInMainTranscript(run, 'node-hello', new Set(['node-hello'])), false);
}

function testDirectResponseRunIsSideViewNotMainTranscriptWithoutNode() {
  const run = chatRun({
    kind: 'direct_response',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello'), true);
  assert.equal(isRunVisibleInMainTranscript(run, 'node-hello', new Set(['node-hello'])), false);
}

function testDirectResponseRunNeverEntersMainTranscriptEvenWithTargetNode() {
  const run = chatRun({
    kind: 'direct_response',
    anchorNodeId: 'node-hello',
    nodeId: 'node-response',
    targetNodeId: 'node-response',
  });

  assert.equal(isRunVisibleInMainTranscript(run, 'node-response', new Set(['node-hello'])), false);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-response', new Set(['node-hello'])), false);
}

function testDetachedChatRunStaysInMainTranscriptDuringPreTargetPhase() {
  const run = chatRun({
    kind: 'chat',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello'), false);
  assert.equal(isRunVisibleInMainTranscript(run, 'node-hello', new Set(['node-hello'])), true);
}

testChildRunIsHiddenFromParentTranscript();
testChildRunIsVisibleOnItsOwnBranch();
testExistingBranchRunIsVisibleWhenTargetIsInHistory();
testPreTargetRunStillBelongsToSelectedAnchor();
testNonStreamingRunDoesNotBlockBranch();
testDetachedBackgroundRunIsSideViewNotMainTranscript();
testDirectResponseRunIsSideViewNotMainTranscriptWithoutNode();
testDirectResponseRunNeverEntersMainTranscriptEvenWithTargetNode();
testDetachedChatRunStaysInMainTranscriptDuringPreTargetPhase();

console.log('runVisibility tests passed');

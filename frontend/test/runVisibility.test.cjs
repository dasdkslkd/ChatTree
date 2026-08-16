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
  getStoppableRunIdsForSelectedBranch,
  isRunBlockingSelectedBranch,
  isRunStoppableFromSelectedBranch,
  isRunVisibleInSelectedTranscript,
  shouldPatchRunIntoMainConversation,
} = require('../src/utils/runVisibility.ts');

function chatRun(overrides = {}) {
  return {
    runId: overrides.runId ?? 'run-1',
    kind: 'chat',
    status: 'streaming',
    anchorNodeId: null,
    nodeId: null,
    targetNodeId: null,
    createdByRunId: null,
    cancellationParentRunId: null,
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

  assert.equal(isDetachedRunView(run, 'node-hello', new Set(['node-hello'])), true);
}

function testDirectResponseRunIsSideViewNotMainTranscriptWithoutNode() {
  const run = chatRun({
    kind: 'direct_response',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello', new Set(['node-hello'])), true);
}

function testDirectResponseRunNeverEntersMainTranscriptEvenWithTargetNode() {
  const run = chatRun({
    kind: 'direct_response',
    anchorNodeId: 'node-hello',
    nodeId: 'node-response',
    targetNodeId: 'node-response',
  });

  assert.equal(isRunBlockingSelectedBranch(run, 'node-response', new Set(['node-hello'])), false);
}

function testDetachedChatRunStaysInMainTranscriptDuringPreTargetPhase() {
  const run = chatRun({
    kind: 'chat',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello', new Set(['node-hello'])), false);
}

function testPendingChatRunStopsBlockingAnchorAfterTargetLands() {
  const run = chatRun({
    kind: 'chat',
    status: 'streaming',
    anchorNodeId: 'node-hello',
    nodeId: 'node-new',
    targetNodeId: 'node-new',
    pendingUserMessage: '新的用户消息',
  });

  assert.equal(isRunVisibleInSelectedTranscript(run, 'node-hello', new Set(['node-hello'])), false);
  assert.equal(isRunBlockingSelectedBranch(run, 'node-hello', new Set(['node-hello'])), false);
}

function testDetachedSubagentIsNotStoppedFromSelectedAnchorWithoutOwnership() {
  const run = chatRun({
    kind: 'subagent',
    status: 'streaming',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isRunBlockingSelectedBranch(run, 'node-hello', new Set(['node-hello'])), false);
  assert.equal(isRunStoppableFromSelectedBranch(run, 'node-hello', new Set(['node-hello'])), false);
}

function testDetachedRunIsVisibleWhenAnchorIsInSelectedBranchHistory() {
  const run = chatRun({
    kind: 'subagent',
    status: 'streaming',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-child', new Set(['node-root', 'node-hello', 'node-child'])), true);
  assert.equal(isRunStoppableFromSelectedBranch(run, 'node-child', new Set(['node-root', 'node-hello', 'node-child'])), false);
}

function testDetachedRunIsHiddenWhenAnchorIsOutsideSelectedBranch() {
  const run = chatRun({
    kind: 'subagent',
    status: 'streaming',
    anchorNodeId: 'node-other',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-child', new Set(['node-root', 'node-hello', 'node-child'])), false);
  assert.equal(isRunStoppableFromSelectedBranch(run, 'node-child', new Set(['node-root', 'node-hello', 'node-child'])), false);
}

function testCommandRunIsSideViewAndStoppableFromAnchor() {
  const run = chatRun({
    kind: 'command',
    status: 'streaming',
    anchorNodeId: 'node-hello',
    nodeId: null,
    targetNodeId: null,
  });

  assert.equal(isDetachedRunView(run, 'node-hello', new Set(['node-hello'])), true);
  assert.equal(isRunStoppableFromSelectedBranch(run, 'node-hello', new Set(['node-hello'])), false);
}

function testSubagentWithTargetNodeStillUsesSideView() {
  const run = chatRun({
    kind: 'subagent',
    status: 'streaming',
    anchorNodeId: 'node-hello',
    nodeId: 'run-child-node',
    targetNodeId: 'run-child-node',
  });

  assert.equal(isDetachedRunView(run, 'node-hello', new Set(['node-hello'])), true);
  assert.equal(isRunStoppableFromSelectedBranch(run, 'node-hello', new Set(['node-hello'])), false);
}

function testCurrentBranchStopIncludesCancellationChildTreeOnly() {
  const runs = [
    chatRun({
      runId: 'main-chat',
      kind: 'chat',
      anchorNodeId: 'node-root',
      nodeId: 'node-main',
      targetNodeId: 'node-main',
    }),
    chatRun({
      runId: 'visible-background-command',
      kind: 'command',
      createdByRunId: 'main-chat',
      cancellationParentRunId: null,
      anchorNodeId: 'node-main',
    }),
    chatRun({
      runId: 'internal-command',
      kind: 'command',
      createdByRunId: 'main-chat',
      cancellationParentRunId: 'main-chat',
      anchorNodeId: 'node-main',
    }),
    chatRun({
      runId: 'owned-nested-subagent',
      kind: 'subagent',
      createdByRunId: 'internal-command',
      cancellationParentRunId: 'internal-command',
      anchorNodeId: 'node-main',
    }),
    chatRun({
      runId: 'unbound-background-command',
      kind: 'command',
      anchorNodeId: 'node-main',
    }),
  ];

  assert.deepEqual(
    getStoppableRunIdsForSelectedBranch(runs, 'node-main', new Set(['node-root', 'node-main'])),
    ['main-chat', 'internal-command', 'owned-nested-subagent'],
  );
}

function testOnlyChatRunsPatchMainConversation() {
  assert.equal(shouldPatchRunIntoMainConversation(chatRun({ kind: 'chat' })), true);
  assert.equal(shouldPatchRunIntoMainConversation(chatRun({
    kind: 'subagent',
    nodeId: 'run-subagent',
    targetNodeId: 'run-subagent',
  })), false);
  assert.equal(shouldPatchRunIntoMainConversation(chatRun({ kind: 'side_question' })), false);
  assert.equal(shouldPatchRunIntoMainConversation(chatRun({ kind: 'direct_response' })), false);
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
testPendingChatRunStopsBlockingAnchorAfterTargetLands();
testDetachedSubagentIsNotStoppedFromSelectedAnchorWithoutOwnership();
testDetachedRunIsVisibleWhenAnchorIsInSelectedBranchHistory();
testDetachedRunIsHiddenWhenAnchorIsOutsideSelectedBranch();
testCommandRunIsSideViewAndStoppableFromAnchor();
testSubagentWithTargetNodeStillUsesSideView();
testCurrentBranchStopIncludesCancellationChildTreeOnly();
testOnlyChatRunsPatchMainConversation();

console.log('runVisibility tests passed');

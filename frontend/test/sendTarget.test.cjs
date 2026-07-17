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
  resolveSendNodeId,
  resolveSlashStreamNodeId,
  shouldDetachSlashStreamTarget,
} = require(path.join(__dirname, '../src/utils/sendTarget.ts'));

function testEditTargetWinsOverCurrentNode() {
  assert.equal(
    resolveSendNodeId({
      editTargetNodeId: 'root',
      currentNodeId: 'node-hello',
      conversationCurrentNodeId: 'node-openai',
    }),
    'root',
  );
}

function testFallsBackToCurrentNodeThenConversationNode() {
  assert.equal(
    resolveSendNodeId({
      editTargetNodeId: null,
      currentNodeId: 'node-current',
      conversationCurrentNodeId: 'node-conversation',
    }),
    'node-current',
  );
  assert.equal(
    resolveSendNodeId({
      editTargetNodeId: null,
      currentNodeId: null,
      conversationCurrentNodeId: 'node-conversation',
    }),
    'node-conversation',
  );
}

function testDetachedSlashPoliciesDoNotUseStreamTarget() {
  for (const policy of ['anchor_only', 'none']) {
    assert.equal(
      resolveSlashStreamNodeId({
        sendNodeId: 'node-current',
        streamTargetPolicy: policy,
      }),
      undefined,
    );
    assert.equal(shouldDetachSlashStreamTarget(policy), true);
  }
}

function testTargetNodePolicyUsesStreamTarget() {
  assert.equal(
    resolveSlashStreamNodeId({
      sendNodeId: 'node-current',
      streamTargetPolicy: 'target_node',
    }),
    'node-current',
  );
  assert.equal(shouldDetachSlashStreamTarget('target_node'), false);
}

function main() {
  testEditTargetWinsOverCurrentNode();
  testFallsBackToCurrentNodeThenConversationNode();
  testDetachedSlashPoliciesDoNotUseStreamTarget();
  testTargetNodePolicyUsesStreamTarget();
  console.log('sendTarget tests passed');
}

main();

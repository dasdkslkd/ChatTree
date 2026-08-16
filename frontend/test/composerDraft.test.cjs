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
  NEW_COMPOSER_DRAFT_KEY,
  getComposerDraft,
  setComposerDraft,
  removeComposerDraft,
} = require(path.join(__dirname, '../src/utils/composerDraft.ts'));

function testSetGetRemove() {
  setComposerDraft('conv-1', { text: 'hello', editing: null });
  assert.deepEqual(getComposerDraft('conv-1'), { text: 'hello', editing: null });
  removeComposerDraft('conv-1');
  assert.equal(getComposerDraft('conv-1'), undefined);
}

function testKeyIsolation() {
  setComposerDraft('conv-a', { text: 'aaa', editing: null });
  assert.equal(getComposerDraft('conv-b'), undefined);
  removeComposerDraft('conv-a');
}

function testEditingIsStored() {
  setComposerDraft('conv-2', {
    text: '修改中的内容',
    editing: {
      targetNodeId: 'node-9',
      returnNodeId: 'node-2',
      toolPermissionMode: 'auto_approve',
    },
  });
  const draft = getComposerDraft('conv-2');
  assert.equal(draft.text, '修改中的内容');
  assert.equal(draft.editing.targetNodeId, 'node-9');
  assert.equal(draft.editing.returnNodeId, 'node-2');
  assert.equal(draft.editing.toolPermissionMode, 'auto_approve');
  removeComposerDraft('conv-2');
}

function testSetOverwritesWholeDraft() {
  setComposerDraft('conv-3', { text: 'old', editing: null });
  setComposerDraft('conv-3', { text: 'new', editing: null });
  assert.deepEqual(getComposerDraft('conv-3'), { text: 'new', editing: null });
  removeComposerDraft('conv-3');
}

function testNewConversationKey() {
  assert.equal(NEW_COMPOSER_DRAFT_KEY, 'new');
  setComposerDraft(NEW_COMPOSER_DRAFT_KEY, { text: '草稿', editing: null });
  assert.equal(getComposerDraft('new').text, '草稿');
  removeComposerDraft(NEW_COMPOSER_DRAFT_KEY);
  assert.equal(getComposerDraft('new'), undefined);
}

function main() {
  testSetGetRemove();
  testKeyIsolation();
  testEditingIsStored();
  testSetOverwritesWholeDraft();
  testNewConversationKey();
  console.log('composerDraft tests passed');
}

main();

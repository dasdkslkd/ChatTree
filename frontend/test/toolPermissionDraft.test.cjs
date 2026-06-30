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
  createToolPermissionDraft,
  getPendingToolPermissionMode,
  markToolPermissionModeSent,
  syncToolPermissionDraftFromBranch,
  selectToolPermissionMode,
} = require(path.join(__dirname, '../src/utils/toolPermissionDraft.ts'));

function testInitialDraftDoesNotOverrideParent() {
  const draft = createToolPermissionDraft();

  assert.equal(draft.mode, 'ask_always');
  assert.equal(getPendingToolPermissionMode(draft), undefined);
}

function testSelectedModeIsSentOnceThenInherited() {
  const selected = selectToolPermissionMode(createToolPermissionDraft(), 'auto_approve');

  assert.equal(selected.mode, 'auto_approve');
  assert.equal(getPendingToolPermissionMode(selected), 'auto_approve');

  const sent = markToolPermissionModeSent(selected);

  assert.equal(sent.mode, 'auto_approve');
  assert.equal(getPendingToolPermissionMode(sent), undefined);
}

function testBranchModeUpdatesDisplayWhenNoPendingSelection() {
  const draft = syncToolPermissionDraftFromBranch(createToolPermissionDraft(), 'modify_only');

  assert.equal(draft.mode, 'modify_only');
  assert.equal(getPendingToolPermissionMode(draft), undefined);
}

function testBranchModeDoesNotOverwritePendingSelection() {
  const selected = selectToolPermissionMode(createToolPermissionDraft(), 'auto_approve');
  const draft = syncToolPermissionDraftFromBranch(selected, 'modify_only');

  assert.equal(draft.mode, 'auto_approve');
  assert.equal(getPendingToolPermissionMode(draft), 'auto_approve');
}

function testCompletingPreviousSendDoesNotClearNewPendingSelection() {
  const nextSelection = selectToolPermissionMode(createToolPermissionDraft(), 'modify_only');
  const draft = markToolPermissionModeSent(nextSelection, 'auto_approve');

  assert.equal(draft.mode, 'modify_only');
  assert.equal(getPendingToolPermissionMode(draft), 'modify_only');
}

function main() {
  testInitialDraftDoesNotOverrideParent();
  testSelectedModeIsSentOnceThenInherited();
  testBranchModeUpdatesDisplayWhenNoPendingSelection();
  testBranchModeDoesNotOverwritePendingSelection();
  testCompletingPreviousSendDoesNotClearNewPendingSelection();
  console.log('toolPermissionDraft tests passed');
}

main();

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testMainPageDoesNotRenderInlineApprovalGroups() {
  assert.equal(source.includes('<ToolApprovalGroup'), false);
  assert.equal(source.includes('function ToolApprovalGroup'), false);
  assert.equal(source.includes('function ToolApprovalCard'), false);
}

function testInputPopupReceivesUnifiedRunSurface() {
  assert.match(source, /collectPendingToolApprovalPrompts\(approvalPromptRunStates\)/);
}

testMainPageDoesNotRenderInlineApprovalGroups();
testInputPopupReceivesUnifiedRunSurface();

console.log('toolApprovalPlacement tests passed');

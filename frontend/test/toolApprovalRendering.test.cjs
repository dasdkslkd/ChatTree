const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function testToolApprovalCardHasTypedActions() {
  const card = read('src/components/transcript/items/ToolApprovalCard.tsx');
  const renderer = read('src/components/transcript/TranscriptItemRenderer.tsx');
  const list = read('src/components/transcript/TranscriptList.tsx');
  const types = read('src/types/transcript.ts');

  assert.match(card, /item:\s*ToolApprovalItem/);
  assert.match(card, /onApproveTool\?\.\(item\)/);
  assert.match(card, /onRejectTool\?\.\(item\)/);
  assert.match(card, /item\.status === 'awaiting_approval'/);
  assert.match(renderer, /case 'tool_approval'/);
  assert.match(renderer, /onApproveTool=\{onApproveTool\}/);
  assert.match(list, /onApproveTool=\{onApproveTool\}/);
  assert.match(types, /TranscriptToolApprovalActionHandler = \(item: ToolApprovalItem\)/);
}

function testToolApprovalActionUsesDtoToolCallId() {
  const api = read('src/api/message.ts');
  const page = read('src/pages/MainPage.tsx');

  assert.match(api, /tool-approvals\/tool-calls\/\$\{encodeURIComponent\(toolCallId\)\}\/decide/);
  assert.match(api, /conversation_id: conversationId/);
  assert.match(api, /node_id: nodeId/);
  assert.match(page, /messageApi\.approveTool\(conversationId,\s*item\.tool_call_id,\s*item\.node_id\)/);
  assert.match(page, /messageApi\.rejectTool\(conversationId,\s*item\.tool_call_id,\s*item\.node_id\)/);
  assert.match(page, /streamManager\.resumeStream\(\s*conversationId,\s*item\.node_id,\s*item\.run_id/);
  assert.match(page, /缺少 run_id/);
  assert.doesNotMatch(page, /pendingApproval|pending approval|toolApprovals/);
}

testToolApprovalCardHasTypedActions();
testToolApprovalActionUsesDtoToolCallId();
console.log('tool approval rendering tests passed');

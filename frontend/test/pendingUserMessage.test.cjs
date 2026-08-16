const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const transcriptList = fs.readFileSync(path.join(__dirname, '../src/components/transcript/TranscriptList.tsx'), 'utf8');
const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const streamManager = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');

function testPendingUserBubbleRendersImmediateFeedback() {
  // 发送中气泡：复用用户消息视觉样式（accent-soft 渐变 + Markdown 渲染）
  assert.match(transcriptList, /function PendingUserMessage\(\{ content \}: \{ content: string \}\)/);
  assert.match(transcriptList, /linear-gradient\(160deg, color-mix\(in srgb, var\(--accent-soft\) 45%, transparent\)/);
  assert.match(transcriptList, /<MarkdownContent enableMermaid>\{content\}<\/MarkdownContent>/);
  // 状态标签：spinner + 文案 + role=status
  assert.match(transcriptList, /<Loader2 className="h-3 w-3 animate-spin" \/>/);
  assert.match(transcriptList, /发送中/);
  assert.match(transcriptList, /role="status"/);
}

function testTranscriptListAcceptsPendingUserItemsProp() {
  assert.match(transcriptList, /pendingUserItems\?: Array<\{ id: string; content: string \}>/);
  assert.match(transcriptList, /pendingUserItems = \[\],/);
  // 在 transcript 末尾渲染发送中气泡
  assert.match(transcriptList, /\{pendingUserItems\.map\(\(pending\) => \(\s*<PendingUserMessage key=\{pending\.id\} content=\{pending\.content\} \/>\s*\)\)\}/);
}

function testMainPageDerivesPendingItemsFromActiveRuns() {
  // 已落地的用户消息节点 id 收集，用于拦截已显示的 pending 气泡
  assert.match(
    mainPage,
    /const landedNodeIds = new Set\(\s*transcriptItems\s*\.filter\(\(item\): item is UserMessageItem => item\.type === 'user_message'\)\s*\.map\(\(item\) => item\.node_id\),\s*\);/,
  );
  // 只对主会话、待落地的 chat run 显示发送中气泡
  assert.match(
    mainPage,
    /run\.conversationId === currentConversation\.id\s*&& run\.pendingUserMessage\s*&& \(run\.status === 'streaming' \|\| run\.status === 'waiting_approval'\)\s*&& shouldPatchRunIntoMainConversation\(run\)\s*&& \(!run\.nodeId \|\| !landedNodeIds\.has\(run\.nodeId\)\),/,
  );
  assert.match(mainPage, /id: `pending:\$\{run\.runId\}`,/);
  assert.match(mainPage, /content: run\.pendingUserMessage as string,/);
}

function testMainPagePassesPendingItemsToTranscriptList() {
  assert.match(mainPage, /pendingUserItems=\{pendingUserItems\}/);
}

function testStreamStateCarriesPendingUserMessage() {
  // pendingUserMessage 已存在于 StreamState（侧边栏复用），主 transcript 新接入
  assert.match(streamManager, /pendingUserMessage: string \| null;/);
}

testPendingUserBubbleRendersImmediateFeedback();
testTranscriptListAcceptsPendingUserItemsProp();
testMainPageDerivesPendingItemsFromActiveRuns();
testMainPagePassesPendingItemsToTranscriptList();
testStreamStateCarriesPendingUserMessage();
console.log('pendingUserMessage tests passed');

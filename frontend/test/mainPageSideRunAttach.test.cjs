const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testOpeningConversationDoesNotAttachHistoricalSideRuns() {
  assert.equal(source.includes('syncSelectedConversationSideRuns'), false);
  assert.equal(source.includes('runsApi.listConversation'), false);
  assert.equal(source.includes('getVisibleSideRunRecords'), false);
}

function testStreamFinishDoesNotRequestSideRuns() {
  const finishHandler = source.match(/streamManager\.onFinish\([\s\S]*?\n    \}\);/);
  assert.ok(finishHandler, 'MainPage should register stream finish handling');
  assert.equal(finishHandler[0].includes('sideRuns'), false);
  assert.equal(finishHandler[0].includes("include: ['messages'"), false);
  assert.equal(finishHandler[0].includes("include: ['messages', 'branches'"), false);
  assert.equal(finishHandler[0].includes('stream-finished-main'), false);
}

function testStopStreamingDoesNotRefreshLegacyMessages() {
  const stopHandler = source.match(/const handleStopStreaming = useCallback\([\s\S]*?\n  \}, \[currentBranchStoppableRunIds/);
  assert.ok(stopHandler, 'MainPage should register stop-streaming handling');
  assert.equal(stopHandler[0].includes("include: ['messages'"), false);
  assert.equal(stopHandler[0].includes("include: ['messages', 'branches'"), false);
}

function testActiveStreamPollingAttachesCurrentLiveRunsOnly() {
  assert.match(source, /messageApi\.getAllActiveStreams\(\)/);
  assert.match(source, /item\.conversation_id !== currentConversationIdRef\.current/);
  assert.match(source, /item\.kind !== 'chat'/);
  assert.match(source, /streamManager\.resumeStream\(\s*item\.conversation_id/);
}

testOpeningConversationDoesNotAttachHistoricalSideRuns();
testStreamFinishDoesNotRequestSideRuns();
testStopStreamingDoesNotRefreshLegacyMessages();
testActiveStreamPollingAttachesCurrentLiveRunsOnly();

console.log('main page side run attach tests passed');

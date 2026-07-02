const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const stopHandlerMatch = mainPage.match(/const handleStopStreaming = useCallback\(\(\) => \{[\s\S]*?\n  \}, \[/);

assert.ok(stopHandlerMatch, 'MainPage should define handleStopStreaming');

const stopHandler = stopHandlerMatch[0];

assert.equal(
  stopHandler.includes('runsApi.stopConversation'),
  false,
  'current branch stop must not call conversation-wide stop',
);
assert.ok(
  stopHandler.includes('currentBranchStoppableRunIds.map((runId) => streamManager.stopRun(runId))'),
  'current branch stop should stop only branch-scoped stoppable runs',
);

console.log('branchStopScope tests passed');

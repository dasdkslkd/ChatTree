const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/services/streamManager.ts'), 'utf8');

function testResumeDoesNotNoopDisconnectedWaitingApprovalRun() {
  assert.doesNotMatch(source, /if\s*\(\s*runId\s*&&\s*existing\?\.runId\s*===\s*runId\s*\)\s*return/);
  assert.match(source, /existing\?\.status === 'waiting_approval'[\s\S]{0,120}!existing\.abortController\.signal\.aborted/);
}

function testResumeStillRejectsTerminalBackendRuns() {
  assert.match(source, /const run = await runsApi\.get\(runId\)/);
  assert.match(source, /\['queued', 'running', 'waiting_approval', 'stopping'\]\.includes\(run\.status\)/);
}

testResumeDoesNotNoopDisconnectedWaitingApprovalRun();
testResumeStillRejectsTerminalBackendRuns();

console.log('streamManager resume tests passed');

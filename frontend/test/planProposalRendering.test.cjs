const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const cardPath = path.join(__dirname, '../src/components/transcript/items/PlanProposalCard.tsx');
const processPath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx');
const processTimelinePath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessTimeline.tsx');
const transcriptPath = path.join(__dirname, '../src/utils/transcriptItems.ts');

function testNoCompactHistoricalPlanCards() {
  const source = fs.readFileSync(cardPath, 'utf8');
  assert.match(source, /awaiting_approval/);
  assert.doesNotMatch(source, /truncatePlan/);
  assert.doesNotMatch(source, /已批准/);
  assert.doesNotMatch(source, /已驳回/);
  assert.match(source, /批准/);
  assert.match(source, /驳回/);
  assert.match(source, /plan-card/);
  assert.match(source, /plan-card-header/);
  assert.match(source, /plan-card-body/);
  assert.match(source, /plan-card-actions/);
}

function testProcessRendererUsesDedicatedPlanProposalCard() {
  const source = fs.readFileSync(processPath, 'utf8');
  const timelineSource = fs.readFileSync(processTimelinePath, 'utf8');
  assert.match(source, /Array\.isArray\(item\.props\?\.timeline\)/);
  assert.match(source, /AssistantProcessTimeline/);
  assert.doesNotMatch(timelineSource, /block\.type === 'plan_proposal'/);
  assert.doesNotMatch(timelineSource, /<PlanProposalCard/);
}

function testPlanProposalCardMatchesProcessShellContract() {
  const source = fs.readFileSync(cardPath, 'utf8');
  assert.match(source, /transcript-plan-card plan-card/);
  assert.match(source, /awaiting_approval/);
  assert.doesNotMatch(source, /truncatePlan/);
  assert.doesNotMatch(source, /aria-label="复制消息"/);
}

function testTranscriptNoLongerDependsOnStandalonePlanCardItem() {
  const source = fs.readFileSync(transcriptPath, 'utf8');
  assert.match(source, /rawType === 'plan_card'/);
  assert.match(source, /status === 'awaiting_approval'/);
  assert.doesNotMatch(source, /status === 'approved'/);
  assert.doesNotMatch(source, /status === 'rejected'/);
}

testNoCompactHistoricalPlanCards();
testProcessRendererUsesDedicatedPlanProposalCard();
testPlanProposalCardMatchesProcessShellContract();
testTranscriptNoLongerDependsOnStandalonePlanCardItem();
console.log('planProposalRendering tests passed');

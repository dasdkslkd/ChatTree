const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const cardPath = path.join(__dirname, '../src/components/transcript/items/PlanProposalCard.tsx');
const processPath = path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx');
const transcriptPath = path.join(__dirname, '../src/utils/transcriptItems.ts');

function testPlanProposalCardHasExpandedAndCompactStates() {
  const source = fs.readFileSync(cardPath, 'utf8');
  assert.match(source, /status === 'awaiting_approval'/);
  assert.match(source, /truncatePlan/);
  assert.match(source, /批准/);
  assert.match(source, /驳回/);
  assert.match(source, /plan-card/);
  assert.match(source, /plan-card-header/);
  assert.match(source, /plan-card-body/);
  assert.match(source, /plan-card-actions/);
}

function testProcessRendererUsesDedicatedPlanProposalCard() {
  const source = fs.readFileSync(processPath, 'utf8');
  assert.match(source, /Array\.isArray\(item\.props\?\.timeline\)/);
  assert.match(source, /if \(timeline\.length > 0\)/);
  assert.match(source, /block\.type === 'plan_proposal'/);
  assert.match(source, /<PlanProposalCard/);
  assert.doesNotMatch(source, /plan_proposal[\s\S]{0,120}<ToolCallCard/);
}

function testTranscriptNoLongerDependsOnStandalonePlanCardItem() {
  const source = fs.readFileSync(transcriptPath, 'utf8');
  assert.doesNotMatch(source, /type:\s*'plan_card'/);
  assert.doesNotMatch(source, /item_type === 'plan_card'/);
}

testPlanProposalCardHasExpandedAndCompactStates();
testProcessRendererUsesDedicatedPlanProposalCard();
testTranscriptNoLongerDependsOnStandalonePlanCardItem();
console.log('planProposalRendering tests passed');

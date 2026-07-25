const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function read(relativePath) {
  return fs.readFileSync(path.join(__dirname, '..', relativePath), 'utf8');
}

function testPlanRenderingIsTypedAndTopLevel() {
  const renderer = read('src/components/transcript/TranscriptItemRenderer.tsx');
  const process = read('src/components/transcript/items/AssistantProcessItem.tsx');
  const timeline = read('src/components/transcript/items/AssistantProcessTimeline.tsx');
  const transcriptItems = read('src/utils/transcriptItems.ts');

  assert.match(renderer, /PlanQuestionCard/);
  assert.match(renderer, /PlanApprovalCard/);
  const removedCard = ['Plan', 'Card', 'Item'].join('');
  assert.doesNotMatch(process, new RegExp(`${removedCard}|item\\.props\\?\\.timeline`));
  assert.doesNotMatch(timeline, new RegExp(removedCard));
  assert.doesNotMatch(transcriptItems, /rawType|plan_card/);
  const list = read('src/components/transcript/TranscriptList.tsx');
  assert.match(list, /normalizeTranscriptItems\(items\)/);
  assert.doesNotMatch(list, /mergeNodeProcessItems|processByNode/);
  assert.doesNotMatch(list, /compact_with_next_answer|compact_after_process/);
  assert.doesNotMatch(timeline, /allowProcessOnly: true/);
}

function testPlanCardsUseDirectDtoFields() {
  const question = read('src/components/transcript/items/PlanQuestionCard.tsx');
  const approval = read('src/components/transcript/items/PlanApprovalCard.tsx');

  assert.match(question, /item\.question/);
  assert.match(question, /item\.options/);
  assert.match(question, /item\.answer/);
  assert.match(approval, /item\.plan/);
  assert.match(approval, /item\.feedback/);
}

testPlanRenderingIsTypedAndTopLevel();
testPlanCardsUseDirectDtoFields();
console.log('plan rendering tests passed');

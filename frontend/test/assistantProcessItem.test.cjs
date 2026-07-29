const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(
  path.join(__dirname, '../src/components/transcript/items/AssistantProcessItem.tsx'),
  'utf8',
);

function testAssistantProcessUsesTypedBlocksOnly() {
  assert.match(source, /const blocks = Array\.isArray\(item\.blocks\) \? item\.blocks : \[\]/);
  assert.match(source, /block\.type === 'tool_call'/);
  assert.match(source, /streaming: isLast && Boolean\(block\.streaming\)/);
  assert.doesNotMatch(source, /getActiveReasoningKey/);
  assert.doesNotMatch(source, /item\.props\?\.timeline|timeline\.length === 0 && !hasPersistedTimeline/);
  assert.doesNotMatch(source, /appendAssistantContinuations/);
}

testAssistantProcessUsesTypedBlocksOnly();

console.log('assistantProcessItem tests passed');

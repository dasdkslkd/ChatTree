const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/components/transcript/items/TaskNotificationItem.tsx'), 'utf8');

assert.match(source, /export function TaskNotificationItem/);
assert.doesNotMatch(
  fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8'),
  /renderTaskNotificationMessage|renderTaskLedgerStrip/,
  'MainPage should not place task notifications outside transcript projection',
);
assert.match(source, /className="transcript-task-notification w-full my-1 flex flex-col items-start"/);

console.log('taskNotificationPlacement tests passed');

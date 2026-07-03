const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const start = source.indexOf('const renderTaskNotificationMessage');
const end = source.indexOf('const getSideRunGroupLabel', start);

assert.notEqual(start, -1, 'renderTaskNotificationMessage should exist');
assert.notEqual(end, -1, 'renderTaskNotificationMessage block should be bounded');

const block = source.slice(start, end);

assert.match(
  block,
  /className="task-notification-row w-full my-1 flex flex-col items-start"/,
  'task notification row should align to the same left column as assistant messages',
);
assert.doesNotMatch(
  block,
  /className="w-full my-1 flex justify-center"/,
  'task notification row should not be centered',
);

console.log('taskNotificationPlacement tests passed');

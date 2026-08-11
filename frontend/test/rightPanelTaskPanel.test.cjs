const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

assert.match(mainPage, /rightPanelView,\s*setRightPanelView\]\s*=\s*useState<'outline' \| 'side' \| 'tasks' \| 'files'>/);
assert.match(mainPage, /taskStateCoordinator\.refresh\(conversationId\)/);
assert.match(mainPage, /taskStateCoordinator\.subscribe\(conversationId/);
assert.match(mainPage, /createTaskPanelItem\(activeTask\)/);
assert.match(mainPage, /rightPanelView === 'tasks'/);
assert.match(mainPage, />\s*任务\s*</);
assert.match(mainPage, /taskContextMode === 'attached'/);
assert.match(mainPage, /setTaskContextMode\(checked \? 'attached' : 'detached'\)/);
assert.match(mainPage, /task_context_mode:\s*taskContextMode/);
assert.match(mainPage, /当前对话暂无任务/);
assert.doesNotMatch(mainPage, /activeTaskService/);
assert.doesNotMatch(
  mainPage,
  /refreshActiveTask\(conversationId\);\s*}\s*, \[currentBranchStreamActivity,[^\]]*sideRunActivity\]/,
);

console.log('rightPanelTaskPanel tests passed');

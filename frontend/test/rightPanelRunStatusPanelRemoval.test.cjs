const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

assert.equal(mainPage.includes('RunStatusPanel'), false, 'MainPage should not import or render RunStatusPanel');
assert.ok(
  mainPage.includes("'app-right-panel flex flex-col shrink-0 transition-[width] duration-200 overflow-hidden'"),
  'Right panel root should not be the scrolling container',
);
assert.ok(
  mainPage.includes('className="flex shrink-0 items-center gap-2 px-3 pb-3"'),
  'Selected run detail header should stay outside the scrollable body',
);
assert.ok(
  mainPage.includes('className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar"'),
  'Selected run detail body should own vertical scrolling',
);
assert.equal(
  mainPage.includes("if (sideRunTopLevelCount === 0) return;\n    setOutlineCollapsed(false);\n    setRightPanelView('side');"),
  false,
  'Right panel should not automatically switch to the run tab when new side runs appear',
);
assert.ok(
  mainPage.includes("onClick={() => setRightPanelView('side')}"),
  'Users should still be able to open the run tab manually',
);

console.log('rightPanelRunStatusPanelRemoval tests passed');

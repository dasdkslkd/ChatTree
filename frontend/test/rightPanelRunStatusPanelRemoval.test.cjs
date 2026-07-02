const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const mainPage = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

assert.equal(mainPage.includes('RunStatusPanel'), false, 'MainPage should not import or render RunStatusPanel');

console.log('rightPanelRunStatusPanelRemoval tests passed');

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const files = [
  'src/pages/MainPage.tsx',
  'src/api/message.ts',
  'src/services/streamManager.ts',
];

const forbidden = [
  'drainPendingSyntheticInput',
  'getPendingSyntheticInputs',
  'streamSyntheticInput',
  'startSyntheticInputStream',
];

for (const file of files) {
  const text = fs.readFileSync(path.join(root, file), 'utf8');
  for (const token of forbidden) {
    assert.equal(
      text.includes(token),
      false,
      `${file} must not contain frontend synthetic followup scheduler token ${token}`,
    );
  }
}

console.log('noFrontendSyntheticScheduler tests passed');

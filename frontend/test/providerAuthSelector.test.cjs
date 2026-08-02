const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(
  path.join(__dirname, '../src/components/settings/sections/providers.tsx'),
  'utf8',
);

assert.match(source, /value: 'api_key' \| 'codex' \| 'copilot' \| 'claude'/);
assert.match(source, /value=\{currentSubscription \|\| 'api_key'\}/);
assert.match(source, /v === 'api_key' \? undefined/);
assert.doesNotMatch(source, /opt\.value \|\| 'none'/);

console.log('providerAuthSelector tests passed');

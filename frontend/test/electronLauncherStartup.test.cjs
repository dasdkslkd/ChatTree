const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../electron/main.cjs'), 'utf8');

assert.match(
  source,
  /CHATTREE_CLIENT_PORT: "0"/,
  'Electron should ask the OS to allocate the launcher port',
);
assert.match(
  source,
  /line\.startsWith\(LAUNCHER_READY_PREFIX\)/,
  'Electron should consume readiness from the launcher child process',
);
assert.match(
  source,
  /const url = new URL\(apiPath, launcherOrigin\)/,
  'Launcher API requests should use the reported origin',
);
assert.match(
  source,
  /view\.webContents\.loadURL\(`\$\{launcherOrigin\}\/s\//,
  'Profile views should use the reported origin',
);

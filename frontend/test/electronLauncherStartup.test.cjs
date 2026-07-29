const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../electron/main.cjs'), 'utf8');
const shellSource = fs.readFileSync(path.join(__dirname, '../electron/shell.js'), 'utf8');
const packageConfig = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));

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
assert.match(
  source,
  /env\.CHATTREE_SERVER_BINARY = `"\$\{cmd\}" server`;/,
  'The local Server should reuse the packaged Launcher runtime',
);
assert.match(
  source,
  /const \{ autoUpdater \} = require\("electron-updater"\)/,
  'The packaged desktop app should use electron-updater',
);
assert.match(
  source,
  /autoUpdater\.checkForUpdates\(\)/,
  'The packaged desktop app should check GitHub Releases on startup',
);
assert.match(
  source,
  /await stopLauncher\(\);[\s\S]*autoUpdater\.quitAndInstall\(false, true\)/,
  'Installing an update must stop the Launcher before restarting',
);
assert.equal(packageConfig.dependencies['electron-updater'], '6.8.9');
assert.deepEqual(packageConfig.build.publish, [{
  provider: 'github',
  owner: 'dasdkslkd',
  repo: 'ChatTree',
  private: true,
  releaseType: 'draft',
}]);
assert.deepEqual(packageConfig.build.nsis, {
  oneClick: false,
  allowToChangeInstallationDirectory: true,
});
assert.deepEqual(packageConfig.build.mac.target, ['dmg', 'zip']);
assert.equal(packageConfig.build.extraResources, undefined);
assert.deepEqual(packageConfig.build.win.extraResources, [{
  from: '../dist/chattree-launcher.exe',
  to: 'chattree-launcher.exe',
}]);
assert.deepEqual(packageConfig.build.mac.extraResources, [{
  from: '../dist/chattree-launcher',
  to: 'chattree-launcher',
}]);
assert.deepEqual(
  packageConfig.build.linux.extraResources,
  packageConfig.build.mac.extraResources,
  'Each platform should package only its shared Launcher runtime',
);
assert.match(
  source,
  /async function createShellWindow\(\)[\s\S]*await shellWindow\.loadFile/,
  'Electron should wait for the shell renderer before publishing the initial tab',
);
assert.match(
  source,
  /await createShellWindow\(\);\s+await addTab\("local", "Local", "local"\);/,
  'Electron should open the Local profile as the initial tab',
);
assert.match(
  source,
  /if \(tabs\[profileId\] !== tab\) return;/,
  'A tab closed during connection must not be recreated by a late result',
);
assert.match(
  source,
  /if \(tab\.kind === "local"\) return;/,
  'Closing a Local tab should keep its application-owned Server session alive',
);
assert.match(
  shellSource,
  /正在启动本地 Server 并完成握手/,
  'The Local connection overlay should describe local startup instead of SSH',
);

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../electron/main.cjs'), 'utf8');
const preloadSource = fs.readFileSync(path.join(__dirname, '../electron/preload.cjs'), 'utf8');
const shellSource = fs.readFileSync(path.join(__dirname, '../electron/shell.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const packageConfig = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const connectProfileSource = source.slice(
  source.indexOf('async function connectProfile'),
  source.indexOf('function sshProfileId'),
);

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
  /app\.isPackaged\s+\? path\.join\(process\.resourcesPath, "frontend"\)/,
  'The packaged Launcher must receive a real frontend directory outside app.asar',
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
assert.deepEqual(packageConfig.build.extraResources, [{
  from: 'dist',
  to: 'frontend',
}]);
assert.equal(packageConfig.build.files.includes('dist/**/*'), false);
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
  connectProfileSource,
  /envelope\?\.code === "server_identity_changed"/,
  'A changed Local Server identity should enter the explicit rebind flow',
);
assert.match(
  connectProfileSource,
  /expected_server_instance_id: observed/,
  'Local rebind must confirm the Server identity reported by the Launcher',
);
assert.match(
  connectProfileSource,
  /result\.data\.error\.message/,
  'Local connection failures should read the owned Launcher error envelope',
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
assert.match(
  source,
  /dialog\.showOpenDialog\(shellWindow,\s*\{[\s\S]*properties: \["openDirectory", "createDirectory"\]/,
  'Electron should open a native project directory picker',
);
assert.match(
  preloadSource,
  /selectProjectFolder: \(\) => ipcRenderer\.invoke\("project:select-folder"\)/,
  'The native directory picker should be exposed to profile views',
);
assert.match(
  source,
  /ipcMain\.on\("theme:set",[\s\S]*nativeTheme\.themeSource = theme/,
  'The appearance preference should drive the native window frame',
);
assert.match(
  preloadSource,
  /setTheme: \(theme\) => ipcRenderer\.send\("theme:set", theme\)/,
  'Profile views should report the appearance preference to the native shell',
);
// ── 启动期主题恢复：launcher 连接等待/壳层标签页/窗口边框在 React 加载前即为正确主题
assert.match(
  source,
  /function loadThemePreference\(\)/,
  'Electron should load the persisted appearance preference from disk',
);
assert.match(
  source,
  /function saveThemePreference\(theme\)/,
  'Electron should persist the appearance preference to disk',
);
assert.match(
  source,
  /ipcMain\.on\("theme:set",[\s\S]*saveThemePreference\(theme\);/,
  'theme:set should persist the native theme preference',
);
assert.match(
  source,
  /Menu\.setApplicationMenu\(null\);\s+nativeTheme\.themeSource = loadThemePreference\(\);\s+registerIpc\(\);/,
  'whenReady should restore the theme before any window or IPC is set up',
);
assert.match(
  indexSource,
  /window\.electronAPI\.setTheme\(theme\);/,
  'The first-paint inline script should push the theme to the native shell',
);

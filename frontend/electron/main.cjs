const { app, BrowserWindow, ipcMain, WebContentsView, Menu, dialog, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const http = require("http");
const { createInterface } = require("readline");

const LAUNCHER_READY_PREFIX = "CHATTREE_LAUNCHER_READY ";
const CLIENT_HOME = path.join(app.getPath("userData"), "client");
const FRONTEND_DIST = app.isPackaged
  ? path.join(process.resourcesPath, "frontend")
  : path.join(__dirname, "..", "dist");
const PROJECT_ROOT = path.join(__dirname, "..", "..");
const LOG_DIR = path.join(app.getPath("userData"), "logs");
const LOG_FILE = path.join(LOG_DIR, "launcher.log");

let launcherProcess = null;
let launcherOrigin = null;
let logStream = null;
let shellWindow = null;
let installUpdateOnQuit = false;
const tabs = {};
let activeTabId = null;

// ── Launcher lifecycle ────────────────────────────────────────────────

function isDevMode() {
  return !app.isPackaged;
}

function startAutoUpdater() {
  if (isDevMode()) return;

  const log = (message) => {
    const line = `[updater] ${message}\n`;
    process.stdout.write(line);
    if (logStream && !logStream.destroyed) {
      logStream.write(line);
    }
  };

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowPrerelease = app.getVersion().includes("-");
  autoUpdater.on("error", (error) => log(`error: ${error.message}`));
  autoUpdater.on("checking-for-update", () => log("checking for update"));
  autoUpdater.on("update-available", (info) => log(`downloading ${info.version}`));
  autoUpdater.on("update-not-available", (info) => log(`up to date: ${info.version}`));
  autoUpdater.on("download-progress", (progress) => {
    log(`downloaded ${progress.percent.toFixed(1)}%`);
  });
  autoUpdater.on("update-downloaded", async (info) => {
    log(`downloaded ${info.version}`);
    const { response } = await dialog.showMessageBox(shellWindow, {
      type: "info",
      buttons: ["立即重启", "退出时安装"],
      defaultId: 0,
      cancelId: 1,
      message: `ChatTree ${info.version} 已下载`,
      detail: "立即重启安装，或继续工作并在退出应用时自动安装。",
    });
    if (response === 0) {
      installUpdateOnQuit = true;
      app.quit();
    }
  });

  autoUpdater.checkForUpdates().catch((error) => {
    log(`check failed: ${error.message}`);
  });
}

function resolveLauncherCmd() {
  const devBinary = process.env.CHATTREE_LAUNCHER_BINARY;
  if (devBinary) return [devBinary];

  if (isDevMode()) {
    return [process.platform === "win32" ? "python" : "python3", "-m", "client_launcher"];
  }

  const ext = process.platform === "win32" ? ".exe" : "";
  const resourcesPath = process.resourcesPath || path.join(__dirname, "..", "..");
  const binaryPath = path.join(resourcesPath, `chattree-launcher${ext}`);
  if (!fs.existsSync(binaryPath)) {
    throw new Error(
      `Launcher binary not found at ${binaryPath}. The app may be improperly installed.`,
    );
  }
  return [binaryPath];
}

function startLauncher() {
  const [cmd, ...args] = resolveLauncherCmd();

  const env = {
    ...process.env,
    CHATTREE_CLIENT_HOME: CLIENT_HOME,
    CHATTREE_CLIENT_PORT: "0",
    CHATTREE_FRONTEND_DIST: FRONTEND_DIST,
  };
  if (!isDevMode()) {
    env.CHATTREE_SERVER_BINARY = `"${cmd}" server`;
  }

  fs.mkdirSync(LOG_DIR, { recursive: true });
  logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });
  logStream.write(`\n--- launcher started ${new Date().toISOString()} ---\n`);

  launcherProcess = spawn(cmd, args, {
    env,
    cwd: isDevMode() ? PROJECT_ROOT : undefined,
    stdio: ["ignore", "pipe", "pipe"],
  });

  const writeLog = (data) => {
    const chunk = `[launcher] ${data}`;
    process.stdout.write(chunk);
    if (logStream && !logStream.destroyed) {
      logStream.write(data);
    }
  };

  return new Promise((resolve, reject) => {
    const fail = (error) => {
      clearTimeout(timeout);
      if (launcherProcess?.exitCode === null && launcherProcess.signalCode === null) {
        launcherProcess.kill();
      }
      reject(error);
    };

    const timeout = setTimeout(() => {
      fail(new Error("Launcher did not report a ready endpoint within 15 seconds"));
    }, 15000);

    launcherProcess.stdout.on("data", writeLog);
    createInterface({ input: launcherProcess.stdout }).on("line", (line) => {
      if (!line.startsWith(LAUNCHER_READY_PREFIX)) return;
      try {
        const ready = JSON.parse(line.slice(LAUNCHER_READY_PREFIX.length));
        if (
          ready.host !== "127.0.0.1"
          || !Number.isInteger(ready.port)
          || ready.port < 1
          || ready.port > 65535
        ) {
          throw new Error("invalid endpoint");
        }
        clearTimeout(timeout);
        launcherOrigin = `http://${ready.host}:${ready.port}`;
        resolve();
      } catch {
        fail(new Error("Launcher reported an invalid ready endpoint"));
      }
    });
    launcherProcess.stderr.on("data", writeLog);
    launcherProcess.on("error", (error) => {
      fail(new Error(`Failed to start launcher: ${error.message}`));
    });
    launcherProcess.on("exit", (code, signal) => {
      if (logStream && !logStream.destroyed) {
        logStream.end(`--- launcher exited ${new Date().toISOString()} ---\n`);
      }
      if (!launcherOrigin) {
        clearTimeout(timeout);
        reject(new Error(`Launcher exited before ready (${signal || code})`));
      } else if (!isQuitting) {
        launcherOrigin = null;
        dialog.showErrorBox("ChatTree 启动器已退出", "Launcher 意外退出，应用将关闭。");
        app.quit();
      }
    });
  });
}

async function stopLauncher() {
  if (!launcherProcess) return;
  const processToStop = launcherProcess;

  if (processToStop.exitCode === null && launcherOrigin) {
    try {
      await launcherApi("/client/v1/shutdown", "POST");
    } catch (_) {
      /* ignore */
    }
  }

  if (processToStop.exitCode === null && processToStop.signalCode === null) {
    await new Promise((resolve) => {
      const onExit = () => resolve();
      processToStop.once("exit", onExit);
      setTimeout(onExit, 5000);
    });
  }

  if (processToStop.exitCode === null && processToStop.signalCode === null) {
    processToStop.kill();
  }
  launcherProcess = null;
  launcherOrigin = null;
}

// ── HTTP helpers ───────────────────────────────────────────────────────

function launcherApi(apiPath, method = "GET", body = null) {
  if (!launcherOrigin) {
    return Promise.reject(new Error("Launcher is not ready"));
  }
  return new Promise((resolve, reject) => {
    const url = new URL(apiPath, launcherOrigin);
    const options = { method, headers: {} };
    if (body) {
      const json = JSON.stringify(body);
      options.headers["Content-Type"] = "application/json";
      options.headers["Content-Length"] = Buffer.byteLength(json);
    }
    const req = http.request(url, options, (res) => {
      let resBody = "";
      res.on("data", (chunk) => { resBody += chunk; });
      res.on("end", () => {
        let parsed = null;
        try { parsed = JSON.parse(resBody); } catch {}
        resolve({ status: res.statusCode, data: parsed });
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

async function connectProfile(profileId) {
  const url = `/client/v1/profiles/${encodeURIComponent(profileId)}/connect`;
  let result = await launcherApi(url, "POST");
  const envelope = result.data?.error;
  const observed = envelope?.details?.observed_server_instance_id;
  if (result.status === 409 && envelope?.code === "server_identity_changed" && observed) {
    const { response } = await dialog.showMessageBox(shellWindow, {
      type: "question",
      buttons: ["重新绑定并连接", "取消"],
      defaultId: 0,
      cancelId: 1,
      message: "本地 Server 身份已变化（数据目录可能被重建）。\n重新绑定到新的 Server 实例？",
    });
    if (response === 0) {
      result = await launcherApi(url, "POST", {
        rebind: true,
        expected_server_instance_id: observed,
      });
    }
  }
  if (result.status === 200) {
    return { serverInstanceId: result.data?.server_instance_id || null };
  }
  if (result.status === 409 && result.data?.error) {
    throw Object.assign(new Error(result.data.error.message), {
      code: result.data.error.code,
    });
  }
  const detail = result.data?.error?.message || `HTTP ${result.status}`;
  throw new Error(`Failed to connect profile: ${detail}`);
}

// Mirror of client_launcher.models.ssh_profile_id: "ssh:" + urlsafe_b64(host) without padding.
function sshProfileId(alias) {
  return "ssh:" + Buffer.from(alias, "utf-8").toString("base64url");
}

async function connectSshHost(alias) {
  const url = `/client/v1/ssh/hosts/${encodeURIComponent(alias)}/connect`;
  let result = await launcherApi(url, "POST");
  // 远程 server 身份变化（如数据目录被重建）：确认后重新绑定
  const envelope = result.data?.error;
  const observed = envelope?.details?.observed_server_instance_id;
  if (result.status === 409 && envelope?.code === "server_identity_changed" && observed) {
    const { response } = await dialog.showMessageBox(shellWindow, {
      type: "question",
      buttons: ["重新绑定并连接", "取消"],
      defaultId: 0,
      cancelId: 1,
      message: `远程 server 身份已变化（数据目录可能被重建）。\n重新绑定到新的 server 实例并连接 ${alias}？`,
    });
    if (response === 0) {
      result = await launcherApi(url, "POST", {
        rebind: true,
        expected_server_instance_id: observed,
      });
    }
  }
  if (result.status === 200 && result.data) {
    return { serverInstanceId: result.data.session?.server_instance_id || null };
  }
  if (result.status === 409 && result.data) {
    throw Object.assign(new Error(result.data.message || result.data.error?.message || "Server already connected"), {
      code: result.data.code || result.data.error?.code,
    });
  }
  const detail = result.data?.error?.message || `HTTP ${result.status}`;
  throw new Error(`Failed to connect SSH host: ${detail}`);
}

async function getSshHosts() {
  const result = await launcherApi("/client/v1/ssh/hosts");
  if (result.status === 200 && result.data) {
    return result.data.hosts || [];
  }
  return [];
}

// ── Tab management ─────────────────────────────────────────────────────

function handleShortcut(event, input) {
  if (input.type !== "keyDown") return;
  const ctrl = input.control || input.meta;
  if (!ctrl) return;

  if (input.key === "t") {
    event.preventDefault();
    showNavigator();
  } else if (input.key === "w") {
    event.preventDefault();
    if (activeTabId) closeTab(activeTabId);
  } else if (input.key === "tab") {
    event.preventDefault();
    const ids = Object.keys(tabs);
    if (ids.length < 2) return;
    const idx = ids.indexOf(activeTabId);
    const next = input.shift
      ? ids[(idx - 1 + ids.length) % ids.length]
      : ids[(idx + 1) % ids.length];
    switchTab(next);
  } else if (input.key === "r") {
    event.preventDefault();
    if (activeTabId && tabs[activeTabId]?.view) {
      tabs[activeTabId].view.webContents.reload();
    }
  }
}

function createTabView(profileId) {
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  view.webContents.loadURL(`${launcherOrigin}/s/${encodeURIComponent(profileId)}`);
  view.webContents.on("before-input-event", handleShortcut);
  return view;
}

function emitTabsUpdated() {
  const tabList = Object.values(tabs).map(t => ({
    id: t.id,
    label: t.label,
    kind: t.kind,
    alias: t.alias || null,
    status: t.status,
    error: t.error || null,
  }));
  shellWindow?.webContents.send("tabs:updated", tabList, activeTabId);
}

async function addTab(profileId, label, kind, alias = null, connectFn = null) {
  if (tabs[profileId]) {
    switchTab(profileId);
    return;
  }

  const tab = {
    id: profileId,
    label,
    kind,
    alias,
    status: "disconnected",
    serverInstanceId: null,
    view: null,
  };
  tabs[profileId] = tab;

  tab.status = "connecting";
  switchTab(profileId);

  try {
    const connect = connectFn || (() => connectProfile(profileId));
    const result = await connect();
    if (tabs[profileId] !== tab) return;
    tab.serverInstanceId = result?.serverInstanceId || null;
    tab.status = "ready";
    emitTabsUpdated();
  } catch (err) {
    if (tabs[profileId] !== tab) return;
    tab.status = "error";
    tab.error = err.message;
    emitTabsUpdated();
    return;
  }

  tab.view = createTabView(profileId);
  tab.view.setVisible(false);
  shellWindow?.contentView.addChildView(tab.view);
  emitTabsUpdated();
  switchTab(profileId);
}

function switchTab(profileId) {
  const tab = tabs[profileId];
  if (!tab) return;

  if (activeTabId && tabs[activeTabId]?.view) {
    tabs[activeTabId].view.setVisible(false);
  }

  activeTabId = profileId;
  if (tab.view) {
    tab.view.setVisible(true);
    layoutActiveTab();
  }
  emitTabsUpdated();
}

function showNavigator() {
  if (activeTabId && tabs[activeTabId]?.view) {
    tabs[activeTabId].view.setVisible(false);
  }
  activeTabId = null;
  emitTabsUpdated();
}

async function closeTab(profileId) {
  const tab = tabs[profileId];
  if (!tab) return;

  const { serverInstanceId } = tab;

  if (tab.view) {
    shellWindow?.contentView.removeChildView(tab.view);
    tab.view.webContents.close();
  }

  delete tabs[profileId];

  const wasActive = activeTabId === profileId;
  if (wasActive) {
    const remaining = Object.keys(tabs);
    if (remaining.length > 0) {
      switchTab(remaining[0]);
    } else {
      activeTabId = null;
    }
  }
  emitTabsUpdated();

  // Local Server belongs to the application session, not to its tab.
  if (tab.kind === "local") return;

  if (serverInstanceId) {
    await launcherApi(
      `/client/v1/profiles/${encodeURIComponent(profileId)}/server/stop`,
      "POST",
      { expected_server_instance_id: serverInstanceId, timeout_seconds: 15 },
    ).catch(() => {});
  }
  await launcherApi(`/client/v1/profiles/${encodeURIComponent(profileId)}/disconnect`, "POST").catch(() => {});
}

function layoutActiveTab() {
  if (!shellWindow || !activeTabId) return;
  const tab = tabs[activeTabId];
  if (!tab?.view) return;
  const bounds = shellWindow.getContentBounds();
  tab.view.setBounds({ x: 0, y: 40, width: bounds.width, height: bounds.height - 40 });
}

// ── Window creation ────────────────────────────────────────────────────

async function createShellWindow() {
  shellWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  shellWindow.webContents.on("before-input-event", handleShortcut);
  shellWindow.on("resize", layoutActiveTab);
  shellWindow.on("closed", () => {
    shellWindow = null;
  });
  await shellWindow.loadFile(path.join(__dirname, "shell.html"));
}

// ── IPC handlers ───────────────────────────────────────────────────────

function registerIpc() {
  ipcMain.handle("tab:connect-local", async () => {
    await addTab("local", "Local", "local");
  });

  ipcMain.handle("tab:connect-ssh", async (_event, alias) => {
    const profileId = sshProfileId(alias);
    await addTab(profileId, alias, "ssh", alias, () => connectSshHost(alias));
  });

  ipcMain.handle("tab:close", async (_event, tabId) => {
    await closeTab(tabId);
  });

  ipcMain.handle("tab:switch", async (_event, tabId) => {
    switchTab(tabId);
  });

  ipcMain.handle("tab:show-navigator", async () => {
    showNavigator();
    const sshHosts = await getSshHosts();
    return { sshHosts };
  });

  ipcMain.handle("tab:list", () => {
    return Object.values(tabs).map(t => ({ id: t.id, label: t.label, status: t.status }));
  });

  ipcMain.handle("navigator:get-data", async () => {
    const sshHosts = await getSshHosts();
    return { sshHosts };
  });

  ipcMain.handle("project:select-folder", async () => {
    const result = await dialog.showOpenDialog(shellWindow, {
      title: "选择项目文件夹",
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled ? null : result.filePaths[0] || null;
  });
}

// ── App lifecycle ──────────────────────────────────────────────────────

// 外链一律交给系统浏览器，应用视图不被导航走
function isExternalUrl(url) {
  return /^https?:\/\//i.test(url);
}

app.on("web-contents-created", (_event, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (isExternalUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    if (launcherOrigin && url.startsWith(launcherOrigin)) return;
    event.preventDefault();
    if (isExternalUrl(url)) shell.openExternal(url);
  });
});

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  registerIpc();
  await startLauncher();
  await createShellWindow();
  await addTab("local", "Local", "local");
  startAutoUpdater();
}).catch((error) => {
  console.error(error);
  dialog.showErrorBox("ChatTree 启动失败", error.message);
  app.quit();
});

app.on("window-all-closed", () => {
  app.quit();
});

let isQuitting = false;
app.on("before-quit", async (event) => {
  if (isQuitting) return;
  event.preventDefault();
  isQuitting = true;
  await stopLauncher();
  if (installUpdateOnQuit) {
    autoUpdater.quitAndInstall(false, true);
  } else {
    app.quit();
  }
});

const { app, BrowserWindow, ipcMain, WebContentsView, Menu } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const http = require("http");

const LAUNCHER_PORT = 8000;
const CLIENT_HOME = path.join(app.getPath("userData"), "client");
const FRONTEND_DIST = path.join(__dirname, "..", "dist");
const PROJECT_ROOT = path.join(__dirname, "..", "..");

let launcherProcess = null;
let shellWindow = null;
const tabs = {};
let activeTabId = null;

// ── Launcher lifecycle ────────────────────────────────────────────────

function isDevMode() {
  return !app.isPackaged;
}

function resolveLauncherCmd() {
  const devBinary = process.env.CHATTREE_LAUNCHER_BINARY;
  if (devBinary) return [devBinary];

  if (isDevMode()) {
    return [process.platform === "win32" ? "python" : "python3", "-m", "client_launcher"];
  }

  const ext = process.platform === "win32" ? ".exe" : "";
  const resourcesPath = process.resourcesPath || path.join(__dirname, "..", "..");
  return [path.join(resourcesPath, `chattree-launcher${ext}`)];
}

function resolveServerBinary() {
  if (isDevMode()) return null;
  const ext = process.platform === "win32" ? ".exe" : "";
  const resourcesPath = process.resourcesPath || path.join(__dirname, "..", "..");
  return path.join(resourcesPath, `chattree-server${ext}`);
}

function startLauncher() {
  const [cmd, ...args] = resolveLauncherCmd();
  const env = {
    ...process.env,
    CHATTREE_CLIENT_HOME: CLIENT_HOME,
    CHATTREE_CLIENT_PORT: String(LAUNCHER_PORT),
    CHATTREE_FRONTEND_DIST: FRONTEND_DIST,
  };
  const serverBinary = resolveServerBinary();
  if (serverBinary) {
    env.CHATTREE_SERVER_BINARY = serverBinary;
  }

  launcherProcess = spawn(cmd, args, {
    env,
    cwd: isDevMode() ? PROJECT_ROOT : undefined,
    stdio: ["ignore", "pipe", "pipe"],
  });

  launcherProcess.stdout.on("data", (data) => {
    process.stdout.write(`[launcher] ${data}`);
  });
  launcherProcess.stderr.on("data", (data) => {
    process.stderr.write(`[launcher] ${data}`);
  });
  launcherProcess.on("error", (err) => {
    console.error("Failed to start launcher:", err.message);
    app.quit();
  });
}

async function stopLauncher() {
  if (!launcherProcess) return;

  // Request graceful shutdown so the launcher can run its lifespan finally
  // block (sessions.close() -> cascades server termination). Ignore errors
  // since the launcher may already be gone.
  try {
    await launcherApi("/client/v1/shutdown", "POST");
  } catch (_) {
    /* ignore */
  }

  // Wait up to 5s for the launcher process to exit on its own.
  await new Promise((resolve) => {
    const onExit = () => resolve();
    launcherProcess.once("exit", onExit);
    setTimeout(onExit, 5000);
  });

  // Force kill if still running.
  if (launcherProcess && launcherProcess.exitCode === null && launcherProcess.signalCode === null) {
    launcherProcess.kill();
  }
  launcherProcess = null;
}

function waitForLauncher(url, timeoutMs = 15000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const check = () => {
      http.get(url, (res) => {
        if (res.statusCode === 200) resolve();
        else retry();
      }).on("error", retry);
    };
    const retry = () => {
      if (Date.now() - start > timeoutMs) {
        reject(new Error(`Launcher not ready after ${timeoutMs}ms`));
      } else {
        setTimeout(check, 200);
      }
    };
    check();
  });
}

// ── HTTP helpers ───────────────────────────────────────────────────────

function launcherApi(apiPath, method = "GET", body = null) {
  return new Promise((resolve, reject) => {
    const url = new URL(apiPath, `http://127.0.0.1:${LAUNCHER_PORT}`);
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
  const result = await launcherApi(`/client/v1/profiles/${profileId}/connect`, "POST");
  if (result.status === 200) return result.data;
  if (result.status === 409 && result.data) {
    throw Object.assign(new Error(result.data.message || "Server already connected"), {
      code: result.data.code,
    });
  }
  throw new Error(`Failed to connect profile: ${result.status}`);
}

async function connectSshHost(alias) {
  const result = await launcherApi(`/client/v1/ssh/hosts/${encodeURIComponent(alias)}/connect`, "POST");
  if (result.status === 200 && result.data) {
    return { profileId: result.data.profile_id, label: alias };
  }
  if (result.status === 409 && result.data) {
    throw Object.assign(new Error(result.data.message || "Server already connected"), {
      code: result.data.code,
    });
  }
  throw new Error(`Failed to connect SSH host: ${result.status}`);
}

async function getSshHosts() {
  const result = await launcherApi("/client/v1/ssh/hosts");
  if (result.status === 200 && result.data) {
    return result.data.hosts || [];
  }
  return [];
}

// ── Tab management ─────────────────────────────────────────────────────

function createTabView(profileId) {
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  view.webContents.loadURL(`http://127.0.0.1:${LAUNCHER_PORT}/s/${encodeURIComponent(profileId)}`);
  return view;
}

function emitTabsUpdated() {
  const tabList = Object.values(tabs).map(t => ({ id: t.id, label: t.label, status: t.status, error: t.error || null }));
  shellWindow?.webContents.send("tabs:updated", tabList, activeTabId);
}

async function addTab(profileId, label) {
  if (tabs[profileId]) {
    switchTab(profileId);
    return;
  }

  const tab = {
    id: profileId,
    label,
    status: "disconnected",
    view: null,
  };
  tabs[profileId] = tab;

  tab.status = "connecting";
  switchTab(profileId);

  try {
    await connectProfile(profileId);
    tab.status = "ready";
    emitTabsUpdated();
  } catch (err) {
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

  if (tab.view) {
    shellWindow?.contentView.removeChildView(tab.view);
    tab.view.webContents.close();
  }

  delete tabs[profileId];

  if (activeTabId === profileId) {
    const remaining = Object.keys(tabs);
    if (remaining.length > 0) {
      switchTab(remaining[0]);
    } else {
      activeTabId = null;
    }
  }

  await launcherApi(`/client/v1/profiles/${profileId}/disconnect`, "POST").catch(() => {});
  emitTabsUpdated();
}

function layoutActiveTab() {
  if (!shellWindow || !activeTabId) return;
  const tab = tabs[activeTabId];
  if (!tab?.view) return;
  const bounds = shellWindow.getContentBounds();
  tab.view.setBounds({ x: 0, y: 40, width: bounds.width, height: bounds.height - 40 });
}

// ── Window creation ────────────────────────────────────────────────────

function createShellWindow() {
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

  shellWindow.loadFile(path.join(__dirname, "shell.html"));

  shellWindow.on("resize", layoutActiveTab);
  shellWindow.on("closed", () => {
    shellWindow = null;
  });
}

// ── IPC handlers ───────────────────────────────────────────────────────

function registerIpc() {
  ipcMain.handle("tab:connect-local", async () => {
    await addTab("local", "Local");
  });

  ipcMain.handle("tab:connect-ssh", async (_event, alias) => {
    try {
      const result = await connectSshHost(alias);
      await addTab(result.profileId, result.label);
    } catch (err) {
      console.error(`Failed to connect SSH host ${alias}:`, err.message);
    }
  });

  ipcMain.handle("tab:close", async (_event, tabId) => {
    await closeTab(tabId);
  });

  ipcMain.handle("tab:switch", async (_event, tabId) => {
    switchTab(tabId);
  });

  ipcMain.handle("tab:show-navigator", async () => {
    showNavigator();
  });

  ipcMain.handle("tab:list", () => {
    return Object.values(tabs).map(t => ({ id: t.id, label: t.label, status: t.status }));
  });

  ipcMain.handle("navigator:get-data", async () => {
    const sshHosts = await getSshHosts();
    return { sshHosts };
  });
}

// ── App lifecycle ──────────────────────────────────────────────────────

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  registerIpc();
  startLauncher();
  await waitForLauncher(`http://127.0.0.1:${LAUNCHER_PORT}/client/v1/profiles/local/status`);
  createShellWindow();
});

let isQuitting = false;

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", async (event) => {
  if (isQuitting) return;
  event.preventDefault();
  isQuitting = true;
  await stopLauncher();
  app.quit();
});
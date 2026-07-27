const { app, BrowserWindow, ipcMain, WebContentsView, Menu, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const http = require("http");

const LAUNCHER_PORT = 8000;
const CLIENT_HOME = path.join(app.getPath("userData"), "client");
const FRONTEND_DIST = path.join(__dirname, "..", "dist");
const PROJECT_ROOT = path.join(__dirname, "..", "..");
const LOG_DIR = path.join(app.getPath("userData"), "logs");
const LOG_FILE = path.join(LOG_DIR, "launcher.log");

let launcherProcess = null;
let logStream = null;
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
  const binaryPath = path.join(resourcesPath, `chattree-launcher${ext}`);
  if (!fs.existsSync(binaryPath)) {
    throw new Error(
      `Launcher binary not found at ${binaryPath}. The app may be improperly installed.`,
    );
  }
  return [binaryPath];
}

function resolveServerBinary() {
  if (isDevMode()) return null;
  const ext = process.platform === "win32" ? ".exe" : "";
  const resourcesPath = process.resourcesPath || path.join(__dirname, "..", "..");
  const binaryPath = path.join(resourcesPath, `chattree-server${ext}`);
  if (!fs.existsSync(binaryPath)) {
    throw new Error(
      `Server binary not found at ${binaryPath}. The app may be improperly installed.`,
    );
  }
  return binaryPath;
}

function startLauncher() {
  let cmd, args;
  try {
    [cmd, ...args] = resolveLauncherCmd();
  } catch (err) {
    console.error(err.message);
    app.quit();
    return;
  }

  const env = {
    ...process.env,
    CHATTREE_CLIENT_HOME: CLIENT_HOME,
    CHATTREE_CLIENT_PORT: String(LAUNCHER_PORT),
    CHATTREE_FRONTEND_DIST: FRONTEND_DIST,
  };
  try {
    const serverBinary = resolveServerBinary();
    if (serverBinary) {
      env.CHATTREE_SERVER_BINARY = serverBinary;
    }
  } catch (err) {
    console.error(err.message);
    app.quit();
    return;
  }

  // Open a persistent log file so launcher output survives app exit and can
  // be inspected for troubleshooting.
  fs.mkdirSync(LOG_DIR, { recursive: true });
  logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });
  logStream.write(`\n--- launcher started ${new Date().toISOString()} ---\n`);

  launcherProcess = spawn(cmd, args, {
    env,
    cwd: isDevMode() ? PROJECT_ROOT : undefined,
    stdio: ["ignore", "pipe", "pipe"],
  });

  const writeLog = (prefix, data) => {
    const chunk = `[launcher] ${data}`;
    process.stdout.write(chunk);
    if (logStream && !logStream.destroyed) {
      logStream.write(data);
    }
  };
  launcherProcess.stdout.on("data", (data) => writeLog("stdout", data));
  launcherProcess.stderr.on("data", (data) => writeLog("stderr", data));
  launcherProcess.on("error", (err) => {
    console.error("Failed to start launcher:", err.message);
    if (logStream && !logStream.destroyed) {
      logStream.write(`Failed to start launcher: ${err.message}\n`);
    }
    app.quit();
  });
  launcherProcess.on("exit", () => {
    if (logStream && !logStream.destroyed) {
      logStream.end(`--- launcher exited ${new Date().toISOString()} ---\n`);
    }
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
  if (result.status === 200) {
    return { serverInstanceId: result.data?.server_instance_id || null };
  }
  if (result.status === 409 && result.data) {
    throw Object.assign(new Error(result.data.message || "Server already connected"), {
      code: result.data.code,
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
  view.webContents.loadURL(`http://127.0.0.1:${LAUNCHER_PORT}/s/${encodeURIComponent(profileId)}`);
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
    tab.serverInstanceId = result?.serverInstanceId || null;
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

  // Capture server instance id before removing the tab so we can request
  // a cooperative server shutdown (not just a tunnel disconnect).
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

  // For ready sessions, stop the remote server first (sends shutdown
  // through the tunnel). For error/connecting sessions, only disconnect.
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

  shellWindow.webContents.on("before-input-event", handleShortcut);
  shellWindow.on("resize", layoutActiveTab);
  shellWindow.on("closed", () => {
    shellWindow = null;
  });
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
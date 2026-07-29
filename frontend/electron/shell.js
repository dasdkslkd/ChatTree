const tabBar = document.getElementById("tabBar");
const newTabBtn = document.getElementById("newTabBtn");
const navigator = document.getElementById("navigator");
const contentArea = document.getElementById("contentArea");
const localCard = document.getElementById("localCard");
const sshHostsList = document.getElementById("sshHostsList");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlayTitle");
const overlaySubtitle = document.getElementById("overlaySubtitle");
const overlayRetry = document.getElementById("overlayRetry");

let tabs = [];
let activeTabId = null;

function renderOverlay() {
  const active = tabs.find(t => t.id === activeTabId);
  if (!active || active.status === "ready" || active.status === "disconnected") {
    overlay.style.display = "none";
    overlay.classList.remove("error");
    overlayRetry.style.display = "none";
    return;
  }
  if (active.status === "connecting") {
    overlay.classList.remove("error");
    overlayRetry.style.display = "none";
    overlayTitle.textContent = `正在连接 ${active.label}…`;
    overlaySubtitle.textContent = active.kind === "local"
      ? "正在启动本地 Server 并完成握手，请稍候"
      : "正在建立 SSH 隧道并握手，请稍候";
    overlay.style.display = "flex";
  } else if (active.status === "error") {
    overlay.classList.add("error");
    overlayTitle.textContent = `连接失败：${active.label}`;
    overlaySubtitle.textContent = active.error || "未知错误";
    overlayRetry.style.display = "inline-block";
    overlay.style.display = "flex";
  }
}

function renderTabs() {
  tabBar.querySelectorAll(".tab").forEach(el => el.remove());
  tabs.forEach(tab => {
    const el = document.createElement("div");
    el.className = "tab" + (tab.id === activeTabId ? " active" : "");
    el.title = tab.label + " — " + tab.status + (tab.error ? " — " + tab.error : "");

    const dot = document.createElement("span");
    dot.className = "status-dot " + tab.status;
    el.appendChild(dot);

    const label = document.createElement("span");
    label.textContent = tab.label;
    el.appendChild(label);

    const closeBtn = document.createElement("button");
    closeBtn.className = "close-btn";
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      window.electronAPI.closeTab(tab.id);
    });
    el.appendChild(closeBtn);

    el.addEventListener("click", () => {
      window.electronAPI.switchTab(tab.id);
    });

    tabBar.insertBefore(el, newTabBtn);
  });

  if (activeTabId === null) {
    navigator.style.display = "flex";
    overlay.style.display = "none";
  } else {
    navigator.style.display = "none";
    renderOverlay();
  }
}

function renderSshHosts(hosts) {
  sshHostsList.innerHTML = "";
  if (!hosts || hosts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "nav-empty";
    empty.textContent = "未配置 SSH 主机（在设置中添加）";
    sshHostsList.appendChild(empty);
    return;
  }
  hosts.forEach(host => {
    const card = document.createElement("div");
    card.className = "nav-card";
    card.innerHTML = `
      <div class="icon">S</div>
      <div class="info">
        <div class="title">${host}</div>
        <div class="subtitle">SSH 远程连接</div>
      </div>
      <div class="arrow">&rsaquo;</div>
    `;
    card.addEventListener("click", () => {
      window.electronAPI.connectSshHost(host);
    });
    sshHostsList.appendChild(card);
  });
}

async function loadNavigatorData() {
  if (!window.electronAPI) return;
  try {
    const data = await window.electronAPI.getNavigatorData();
    renderSshHosts(data.sshHosts || []);
  } catch (e) {
    console.error("Failed to load navigator data:", e);
  }
}

localCard.addEventListener("click", () => {
  if (!window.electronAPI) return;
  window.electronAPI.connectLocal();
});

newTabBtn.addEventListener("click", async () => {
  if (window.electronAPI) {
    // showNavigator returns the latest SSH hosts so the navigator page
    // reflects any config changes made since the last load.
    const data = await window.electronAPI.showNavigator();
    if (data && data.sshHosts) {
      renderSshHosts(data.sshHosts);
    }
  } else {
    navigator.style.display = "flex";
  }
});

overlayRetry.addEventListener("click", async () => {
  const active = tabs.find(t => t.id === activeTabId);
  if (!active) return;
  const { kind, alias } = active;
  await window.electronAPI.closeTab(active.id);
  if (kind === "local") {
    window.electronAPI.connectLocal();
  } else if (kind === "ssh" && alias) {
    window.electronAPI.connectSshHost(alias);
  }
});

document.addEventListener("DOMContentLoaded", () => {
  if (window.electronAPI) {
    window.electronAPI.onTabsUpdated((updatedTabs, activeId) => {
      tabs = updatedTabs;
      activeTabId = activeId;
      renderTabs();
    });
    renderTabs();
    loadNavigatorData();
  }
});

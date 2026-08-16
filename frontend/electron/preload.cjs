const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  connectLocal: () => ipcRenderer.invoke("tab:connect-local"),
  connectSshHost: (hostAlias) => ipcRenderer.invoke("tab:connect-ssh", hostAlias),
  closeTab: (tabId) => ipcRenderer.invoke("tab:close", tabId),
  switchTab: (tabId) => ipcRenderer.invoke("tab:switch", tabId),
  showNavigator: () => ipcRenderer.invoke("tab:show-navigator"),
  getTabs: () => ipcRenderer.invoke("tab:list"),
  getNavigatorData: () => ipcRenderer.invoke("navigator:get-data"),
  selectProjectFolder: () => ipcRenderer.invoke("project:select-folder"),
  quitApp: () => ipcRenderer.invoke("app:quit"),
  setTheme: (theme) => ipcRenderer.send("theme:set", theme),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  onTabsUpdated: (callback) => {
    ipcRenderer.on("tabs:updated", (_event, tabs, activeId) => {
      callback(tabs, activeId);
    });
  },
});

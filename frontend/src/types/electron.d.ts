declare global {
  interface Window {
    electronAPI?: {
      connectLocal: () => Promise<void>;
      connectSshHost: (hostAlias: string) => Promise<void>;
      closeTab: (tabId: string) => Promise<void>;
      switchTab: (tabId: string) => Promise<void>;
      getTabs: () => Promise<Array<{ id: string; label: string; status: string }>>;
      getNavigatorData: () => Promise<{ sshHosts: string[] }>;
      selectProjectFolder: () => Promise<string | null>;
      quitApp: () => Promise<void>;
      setTheme: (theme: 'light' | 'dark' | 'system') => void;
      getPathForFile: (file: File) => string;
      onTabsUpdated: (callback: (tabs: Array<{ id: string; label: string; status: string; error: string | null }>, activeId: string | null) => void) => void;
    };
  }
}

export {};

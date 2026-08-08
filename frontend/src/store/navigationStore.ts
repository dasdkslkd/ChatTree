import { create } from 'zustand';

type ChatViewMode = 'chat' | 'tree';
type SettingsSection = 'providers' | 'prompts' | 'ssh' | 'builtin_tools' | 'skills' | 'mcp' | 'agents' | 'plugins' | 'storage';
type AppPage = 'chat' | 'settings';

interface NavigationState {
  activePage: AppPage;
  chatViewMode: ChatViewMode;
  settingsOpen: boolean;
  settingsSection: SettingsSection;
  openChat: () => void;
  setChatViewMode: (mode: ChatViewMode) => void;
  toggleChatViewMode: () => void;
  openSettings: (section?: SettingsSection) => void;
  closeSettings: () => void;
}

export const useNavigationStore = create<NavigationState>((set) => ({
  activePage: 'chat',
  chatViewMode: 'chat',
  settingsOpen: false,
  settingsSection: 'providers',
  openChat: () => set({ activePage: 'chat', settingsOpen: false }),
  setChatViewMode: (mode) => set({ chatViewMode: mode }),
  toggleChatViewMode: () =>
    set((state) => ({
      chatViewMode: state.chatViewMode === 'chat' ? 'tree' : 'chat',
    })),
  openSettings: (section = 'providers') => set({ activePage: 'settings', settingsOpen: true, settingsSection: section }),
  closeSettings: () => set({ activePage: 'chat', settingsOpen: false }),
}));

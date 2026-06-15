import { create } from 'zustand';

type ChatViewMode = 'chat' | 'tree';

interface NavigationState {
  chatViewMode: ChatViewMode;
  settingsOpen: boolean;
  settingsSection: 'providers' | 'prompts';
  setChatViewMode: (mode: ChatViewMode) => void;
  toggleChatViewMode: () => void;
  openSettings: (section?: 'providers' | 'prompts') => void;
  closeSettings: () => void;
}

export const useNavigationStore = create<NavigationState>((set) => ({
  chatViewMode: 'chat',
  settingsOpen: false,
  settingsSection: 'providers',
  setChatViewMode: (mode) => set({ chatViewMode: mode }),
  toggleChatViewMode: () =>
    set((state) => ({
      chatViewMode: state.chatViewMode === 'chat' ? 'tree' : 'chat',
    })),
  openSettings: (section = 'providers') => set({ settingsOpen: true, settingsSection: section }),
  closeSettings: () => set({ settingsOpen: false }),
}));

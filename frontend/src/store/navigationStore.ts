import { create } from 'zustand';

type PageType = 'chat' | 'prompts' | 'settings';
type ChatViewMode = 'chat' | 'tree';

interface NavigationState {
  currentPage: PageType;
  chatViewMode: ChatViewMode;
  setCurrentPage: (page: PageType) => void;
  setChatViewMode: (mode: ChatViewMode) => void;
  toggleChatViewMode: () => void;
}

export const useNavigationStore = create<NavigationState>((set) => ({
  currentPage: 'chat',
  chatViewMode: 'chat',
  setCurrentPage: (page) => set({ currentPage: page }),
  setChatViewMode: (mode) => set({ chatViewMode: mode }),
  toggleChatViewMode: () =>
    set((state) => ({
      chatViewMode: state.chatViewMode === 'chat' ? 'tree' : 'chat',
    })),
}));

import { create } from 'zustand';

type PageType = 'chat' | 'prompts' | 'settings';

interface NavigationState {
  currentPage: PageType;
  setCurrentPage: (page: PageType) => void;
}

export const useNavigationStore = create<NavigationState>((set) => ({
  currentPage: 'chat',
  setCurrentPage: (page) => set({ currentPage: page }),
}));

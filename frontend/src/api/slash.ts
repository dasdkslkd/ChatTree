import { apiClient } from './client';
import type { SlashCommandInfo } from '../types/slash';

export const slashApi = {
  listCommands: async (): Promise<SlashCommandInfo[]> => {
    const response = await apiClient.get('/slash/commands');
    return response.data;
  },
};


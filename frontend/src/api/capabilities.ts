import { apiClient } from './client';

export interface CapabilityAgent {
  name: string;
  description?: string;
}

export interface CapabilityInventory {
  skills: unknown[];
  agents: CapabilityAgent[];
  plugins: unknown[];
}

export const capabilitiesApi = {
  list: async (): Promise<CapabilityInventory> => {
    const response = await apiClient.get('/capabilities');
    return response.data;
  },
};

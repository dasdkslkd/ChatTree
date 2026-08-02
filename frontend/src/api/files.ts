import { apiClient } from './client';

export interface OpenFileResponse {
  path: string;
}

export const filesApi = {
  // 用系统默认软件打开本地文件或目录
  open: async (path: string): Promise<OpenFileResponse> => {
    const response = await apiClient.post('/files/open', { path });
    return response.data;
  },
};

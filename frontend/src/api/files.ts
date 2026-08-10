import { apiClient } from './client';

export interface OpenFileResponse {
  path: string;
}

export interface FileEntry {
  name: string;
  type: 'dir' | 'file';
  size: number;
}

export interface ListDirectoryResponse {
  path: string;
  entries: FileEntry[];
}

export interface FileContentResponse {
  path: string;
  size: number;
  binary: boolean;
  truncated: boolean;
  content: string;
}

export const filesApi = {
  // 用系统默认软件打开本地文件或目录
  open: async (path: string): Promise<OpenFileResponse> => {
    const response = await apiClient.post('/files/open', { path });
    return response.data;
  },
  // 列出目录内容（单层，供文件树懒加载）
  list: async (path: string): Promise<ListDirectoryResponse> => {
    const response = await apiClient.get('/files/list', { params: { path } });
    return response.data;
  },
  // 读取文本文件内容；二进制/超长时只返回元信息
  content: async (path: string): Promise<FileContentResponse> => {
    const response = await apiClient.get('/files/content', { params: { path } });
    return response.data;
  },
  // 在系统文件管理器中定位显示
  reveal: async (path: string): Promise<OpenFileResponse> => {
    const response = await apiClient.post('/files/reveal', { path });
    return response.data;
  },
  // 重命名文件或目录
  rename: async (path: string, newName: string): Promise<OpenFileResponse> => {
    const response = await apiClient.post('/files/rename', { path, new_name: newName });
    return response.data;
  },
  // 删除文件或目录
  delete: async (path: string): Promise<OpenFileResponse> => {
    const response = await apiClient.post('/files/delete', { path });
    return response.data;
  },
};
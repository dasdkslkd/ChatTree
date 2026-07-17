import axios, { type AxiosInstance } from 'axios';

import { getFrontendBootstrap } from '../runtime/frontendBootstrap';
import { normalizeApiError } from './errors';

export const frontendBootstrap = getFrontendBootstrap();

export function serverApiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${frontendBootstrap.apiBase}${suffix}`;
}

export function createApiClient(apiBase: string): AxiosInstance {
  const client = axios.create({
    baseURL: apiBase,
    timeout: 30000,
    headers: {
      'Content-Type': 'application/json',
    },
  });
  client.interceptors.response.use(
    (response) => response,
    (error) => Promise.reject(normalizeApiError(error)),
  );
  return client;
}

export const apiClient = createApiClient(frontendBootstrap.apiBase);

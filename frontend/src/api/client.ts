import axios from 'axios';

import { getFrontendBootstrap } from '../runtime/frontendBootstrap';
import { normalizeApiError } from './errors';

export const frontendBootstrap = getFrontendBootstrap();

export function serverApiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${frontendBootstrap.apiBase}${suffix}`;
}

export const apiClient = axios.create({
  baseURL: frontendBootstrap.apiBase,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(normalizeApiError(error)),
);

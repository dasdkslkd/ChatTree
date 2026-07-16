import axios from 'axios';

export type FrontendBootstrap = {
  profileId?: string;
  apiBase: string;
};

declare global {
  interface Window {
    __CHATTREE_BOOTSTRAP__?: FrontendBootstrap;
  }
}

function normalizeApiBase(value: string): string {
  const normalized = value.trim().replace(/\/+$/, '');
  if (!normalized) throw new Error('ChatTree apiBase must not be empty');
  if (normalized.startsWith('/') || /^https?:\/\//.test(normalized)) {
    return normalized;
  }
  return `/${normalized}`;
}

const injectedBootstrap = typeof window === 'undefined'
  ? undefined
  : window.__CHATTREE_BOOTSTRAP__;

export const frontendBootstrap: FrontendBootstrap = {
  profileId: injectedBootstrap?.profileId,
  apiBase: normalizeApiBase(
    injectedBootstrap?.apiBase ?? '/api/v1',
  ),
};

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
  (error) => {
    if (error.response?.status === 404) {
      console.error('API endpoint not found', error);
    } else if (error.response?.status === 500) {
      console.error('Server error', error);
    }
    return Promise.reject(error);
  },
);

import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';

import {
  composeConnectionAbortSignal,
  connectionScopeRuntime,
  StaleConnectionScopeError,
  type ConnectionScope,
  type ConnectionScopeRuntime,
} from '../runtime/connectionScope';
import { getProfileContext } from '../runtime/profileContext';
import { normalizeApiError, parseModernErrorEnvelope } from './errors';
import {
  CONNECTION_LEASE_HEADER,
  readConnectionLeaseHeader,
} from './connectionLeaseHeader';

export const profileContext = getProfileContext();

export function serverApiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${profileContext.apiBase}${suffix}`;
}

const CONNECTION_SCOPE_CONFIG_KEY = '__chatTreeConnectionScope';

type ScopeRequestConfig = InternalAxiosRequestConfig & {
  [CONNECTION_SCOPE_CONFIG_KEY]?: ConnectionScope;
};

export type ApiClientConnectionRuntime = Pick<
  ConnectionScopeRuntime,
  'current' | 'isActive' | 'invalidate'
>;

function responseScope(response: AxiosResponse): ConnectionScope | undefined {
  return (response.config as ScopeRequestConfig)[CONNECTION_SCOPE_CONFIG_KEY];
}

function assertCurrentResponse(
  runtime: ApiClientConnectionRuntime,
  scope: ConnectionScope | undefined,
  response: AxiosResponse,
): void {
  if (!scope || !runtime.isActive(scope)) throw new StaleConnectionScopeError();
  if (readConnectionLeaseHeader(response.headers) !== scope.leaseId) {
    runtime.invalidate(scope);
    throw new StaleConnectionScopeError();
  }
  const envelope = response.status === 409
    ? parseModernErrorEnvelope(response.status, response.data)
    : null;
  if (envelope?.code === 'stale_connection_epoch') {
    runtime.invalidate(scope);
    throw new StaleConnectionScopeError();
  }
}

function axiosErrorForResponse(response: AxiosResponse): AxiosError {
  return new AxiosError(
    `Request failed with status code ${response.status}`,
    response.status >= 500 ? AxiosError.ERR_BAD_RESPONSE : AxiosError.ERR_BAD_REQUEST,
    response.config,
    response.request,
    response,
  );
}

export function createApiClient(
  apiBase: string,
  runtime: ApiClientConnectionRuntime | null = connectionScopeRuntime,
): AxiosInstance {
  const client = axios.create({
    baseURL: apiBase,
    timeout: 30000,
    headers: { 'Content-Type': 'application/json' },
  });

  if (runtime) {
    client.interceptors.request.use((config) => {
      const scope = runtime.current();
      (config as ScopeRequestConfig)[CONNECTION_SCOPE_CONFIG_KEY] = scope;
      config.headers.set(CONNECTION_LEASE_HEADER, scope.leaseId, true);
      config.signal = composeConnectionAbortSignal(
        config.signal as AbortSignal | undefined,
        scope.signal,
      );
      return config;
    });
  }

  client.interceptors.response.use(
    (response) => {
      if (!runtime) return response;
      assertCurrentResponse(runtime, responseScope(response), response);
      if (response.status >= 400) {
        return Promise.reject(normalizeApiError(axiosErrorForResponse(response)));
      }
      return response;
    },
    (error: unknown) => {
      if (error instanceof StaleConnectionScopeError) return Promise.reject(error);
      if (!runtime) return Promise.reject(normalizeApiError(error));
      if (!axios.isAxiosError(error)) return Promise.reject(normalizeApiError(error));
      const scope = (error.config as ScopeRequestConfig | undefined)?.[
        CONNECTION_SCOPE_CONFIG_KEY
      ];
      if (!scope || !runtime.isActive(scope)) {
        return Promise.reject(new StaleConnectionScopeError());
      }
      if (error.response) assertCurrentResponse(runtime, scope, error.response);
      return Promise.reject(normalizeApiError(error));
    },
  );
  return client;
}

export const apiClient = createApiClient(profileContext.apiBase);

import axios, {
  AxiosError,
  type AxiosInstance,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';

import {
  composeConnectionAbortSignal,
  connectionEpochRuntime,
  StaleConnectionEpochError,
  type ConnectionEpochRuntime,
  type ConnectionEpochToken,
} from '../runtime/connectionEpoch';
import { getFrontendBootstrap } from '../runtime/frontendBootstrap';
import {
  normalizeApiError,
  parseModernErrorEnvelope,
} from './errors';
import {
  CONNECTION_LEASE_HEADER,
  readConnectionLeaseHeader,
} from './connectionLeaseHeader';

export const frontendBootstrap = getFrontendBootstrap();

export function serverApiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${frontendBootstrap.apiBase}${suffix}`;
}

const CONNECTION_EPOCH_CONFIG_KEY = '__chatTreeConnectionEpochToken';

type EpochRequestConfig = InternalAxiosRequestConfig & {
  [CONNECTION_EPOCH_CONFIG_KEY]?: ConnectionEpochToken;
};

export type ApiClientConnectionRuntime = Pick<
  ConnectionEpochRuntime,
  'capture' | 'isCurrent' | 'signalFor' | 'invalidate'
>;

function responseToken(response: AxiosResponse): ConnectionEpochToken | undefined {
  return (response.config as EpochRequestConfig)[CONNECTION_EPOCH_CONFIG_KEY];
}

function assertCurrentResponse(
  runtime: ApiClientConnectionRuntime,
  token: ConnectionEpochToken | undefined,
  response: AxiosResponse,
): void {
  if (!token) throw new StaleConnectionEpochError();
  if (!runtime.isCurrent(token)) throw new StaleConnectionEpochError();
  if (readConnectionLeaseHeader(response.headers) !== token.connectionLeaseId) {
    runtime.invalidate(token);
    throw new StaleConnectionEpochError();
  }
  const envelope = response.status === 409
    ? parseModernErrorEnvelope(response.status, response.data)
    : null;
  if (envelope?.code === 'stale_connection_epoch') {
    runtime.invalidate(token);
    throw new StaleConnectionEpochError();
  }
}

function axiosErrorForResponse(response: AxiosResponse): AxiosError {
  const code = response.status >= 500
    ? AxiosError.ERR_BAD_RESPONSE
    : AxiosError.ERR_BAD_REQUEST;
  return new AxiosError(
    `Request failed with status code ${response.status}`,
    code,
    response.config,
    response.request,
    response,
  );
}

export function createApiClient(
  apiBase: string,
  runtime: ApiClientConnectionRuntime | null = connectionEpochRuntime,
): AxiosInstance {
  const client = axios.create({
    baseURL: apiBase,
    timeout: 30000,
    headers: {
      'Content-Type': 'application/json',
    },
  });

  if (runtime) {
    client.interceptors.request.use((config) => {
      const token = runtime.capture();
      const epochConfig = config as EpochRequestConfig;
      epochConfig[CONNECTION_EPOCH_CONFIG_KEY] = token;
      config.headers.set(
        CONNECTION_LEASE_HEADER,
        token.connectionLeaseId,
        true,
      );
      config.signal = composeConnectionAbortSignal(
        config.signal as AbortSignal | undefined,
        runtime.signalFor(token),
      );
      return config;
    });
  }

  client.interceptors.response.use(
    (response) => {
      if (!runtime) return response;
      assertCurrentResponse(runtime, responseToken(response), response);
      if (response.status >= 400) {
        return Promise.reject(normalizeApiError(axiosErrorForResponse(response)));
      }
      return response;
    },
    (error: unknown) => {
      if (error instanceof StaleConnectionEpochError) {
        return Promise.reject(error);
      }
      if (!runtime) return Promise.reject(normalizeApiError(error));
      if (!axios.isAxiosError(error)) {
        return Promise.reject(normalizeApiError(error));
      }

      const token = (error.config as EpochRequestConfig | undefined)?.[
        CONNECTION_EPOCH_CONFIG_KEY
      ];
      if (!token || !runtime.isCurrent(token)) {
        return Promise.reject(new StaleConnectionEpochError());
      }
      if (error.response) {
        assertCurrentResponse(runtime, token, error.response);
      }
      return Promise.reject(normalizeApiError(error));
    },
  );
  return client;
}

export const apiClient = createApiClient(frontendBootstrap.apiBase);

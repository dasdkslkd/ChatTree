import type { AxiosInstance } from 'axios';

import {
  BoundServerLeaseChangedError,
  EXPECTED_PROTOCOL_VERSION,
  isCanonicalUuid,
} from '../runtime/connectionIdentity';
import { apiClient } from './client';

export { EXPECTED_PROTOCOL_VERSION } from '../runtime/connectionIdentity';
const SERVER_PROBE_TIMEOUT_MS = 5000;
const CONNECTION_LEASE_HEADER = 'X-ChatTree-Connection-Lease-ID';

export type HealthResponse = {
  status: 'ok';
  server_instance_id: string;
  time: number;
};

export type HandshakeResponse = {
  server_instance_id: string;
  protocol_version: number;
  server_version: string;
  platform: string;
  features: string[];
  provider_configured: boolean;
};

export type LeaseGuarded<T> = Readonly<{
  data: T;
  connectionLeaseId: string;
}>;

export type ServerApi = Readonly<{
  health(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HealthResponse>>;
  handshake(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>>;
  assertCompatible(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>>;
}>;

function readSingleHeader(headers: unknown, name: string): unknown {
  if (headers === null || typeof headers !== 'object') return undefined;
  const matches = Object.entries(headers).filter(
    ([key]) => key.toLowerCase() === name.toLowerCase(),
  );
  if (matches.length > 1) return matches.map(([, value]) => value);
  if (matches.length === 1) return matches[0][1];
  const get = (headers as { get?: unknown }).get;
  return typeof get === 'function' ? get.call(headers, name) : undefined;
}

function requireMatchingResponseLease(headers: unknown, expectedLeaseId: string): string {
  const value = readSingleHeader(headers, CONNECTION_LEASE_HEADER);
  if (!isCanonicalUuid(value) || value !== expectedLeaseId) {
    throw new BoundServerLeaseChangedError(
      'Launcher connection changed during Server request',
    );
  }
  return value;
}

export function createServerApi(client: AxiosInstance): ServerApi {
  async function getGuarded<T>(
    path: string,
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<T>> {
    if (!isCanonicalUuid(expectedLeaseId)) {
      throw new BoundServerLeaseChangedError('Expected connection lease is invalid');
    }
    const response = await client.get<T>(path, {
      signal,
      timeout: SERVER_PROBE_TIMEOUT_MS,
      headers: { [CONNECTION_LEASE_HEADER]: expectedLeaseId },
    });
    const connectionLeaseId = requireMatchingResponseLease(
      response.headers,
      expectedLeaseId,
    );
    return { data: response.data, connectionLeaseId };
  }

  const health = (expectedLeaseId: string, signal?: AbortSignal) => (
    getGuarded<HealthResponse>('/health', expectedLeaseId, signal)
  );
  const handshake = (expectedLeaseId: string, signal?: AbortSignal) => (
    getGuarded<HandshakeResponse>('/handshake', expectedLeaseId, signal)
  );
  const assertCompatible = async (
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>> => {
    const guarded = await handshake(expectedLeaseId, signal);
    if (guarded.data.protocol_version !== EXPECTED_PROTOCOL_VERSION) {
      throw new Error(
        `Unsupported ChatTree protocol version: ${guarded.data.protocol_version}`,
      );
    }
    return guarded;
  };

  return { health, handshake, assertCompatible };
}

async function legacyHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await apiClient.get<HealthResponse>('/health', {
    signal,
    timeout: SERVER_PROBE_TIMEOUT_MS,
  });
  return response.data;
}

async function legacyHandshake(signal?: AbortSignal): Promise<HandshakeResponse> {
  const response = await apiClient.get<HandshakeResponse>('/handshake', {
    signal,
    timeout: SERVER_PROBE_TIMEOUT_MS,
  });
  return response.data;
}

async function legacyAssertCompatible(signal?: AbortSignal): Promise<HandshakeResponse> {
  const handshake = await legacyHandshake(signal);
  if (handshake.protocol_version !== EXPECTED_PROTOCOL_VERSION) {
    throw new Error(
      `Unsupported ChatTree protocol version: ${handshake.protocol_version}`,
    );
  }
  return handshake;
}

export const serverApi = {
  health: legacyHealth,
  handshake: legacyHandshake,
  assertCompatible: legacyAssertCompatible,
};

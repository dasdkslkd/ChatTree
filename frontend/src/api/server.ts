import type { AxiosInstance } from 'axios';

import {
  BoundServerLeaseChangedError,
  isCanonicalUuid,
} from '../runtime/connectionIdentity';
import {
  CONNECTION_LEASE_HEADER,
  requireMatchingConnectionLeaseHeader,
} from './connectionLeaseHeader';

const SERVER_PROBE_TIMEOUT_MS = 5000;

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
  handshake(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>>;
}>;

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
    const connectionLeaseId = requireMatchingConnectionLeaseHeader(
      response.headers,
      expectedLeaseId,
    );
    return { data: response.data, connectionLeaseId };
  }

  const handshake = (expectedLeaseId: string, signal?: AbortSignal) => (
    getGuarded<HandshakeResponse>('/handshake', expectedLeaseId, signal)
  );
  return { handshake };
}

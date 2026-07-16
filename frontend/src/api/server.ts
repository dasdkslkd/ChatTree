import { apiClient } from './client';

export const EXPECTED_PROTOCOL_VERSION = 1;
const SERVER_PROBE_TIMEOUT_MS = 5000;

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

async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await apiClient.get<HealthResponse>('/health', {
    signal,
    timeout: SERVER_PROBE_TIMEOUT_MS,
  });
  return response.data;
}

async function getHandshake(signal?: AbortSignal): Promise<HandshakeResponse> {
  const response = await apiClient.get<HandshakeResponse>('/handshake', {
    signal,
    timeout: SERVER_PROBE_TIMEOUT_MS,
  });
  return response.data;
}

async function assertCompatible(signal?: AbortSignal): Promise<HandshakeResponse> {
  const handshake = await getHandshake(signal);
  if (handshake.protocol_version !== EXPECTED_PROTOCOL_VERSION) {
    throw new Error(
      `Unsupported ChatTree protocol version: ${handshake.protocol_version}`,
    );
  }
  return handshake;
}

export const serverApi = {
  health: getHealth,
  handshake: getHandshake,
  assertCompatible,
};

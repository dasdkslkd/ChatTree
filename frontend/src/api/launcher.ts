import type { ProfileContext } from '../runtime/profileContext';
import { createApiClient } from './client';

export type LauncherProfileStatus = {
  profile_id: string;
  status: 'disconnected' | 'connecting' | 'ready' | 'error';
  phase: string | null;
  connection_epoch: number;
  connection_lease_id: string;
  server_instance_id: string | null;
  error: {
    code: string;
    message: string;
    retryable: boolean;
    details?: Record<string, unknown>;
  } | null;
};

export type SshConfigSnapshot = {
  path: string;
  text: string;
  hosts: string[];
  warnings: string[];
};

export type SshHostsResponse = {
  path: string;
  hosts: string[];
  warnings: string[];
};

export type SshHostSessionResponse = {
  profile_id: string;
  host_alias: string;
  session: LauncherProfileStatus;
};

export type LauncherApi = Readonly<{
  getProfileStatus(
    profileId: string,
    signal?: AbortSignal,
  ): Promise<LauncherProfileStatus>;
  connectProfile(
    profileId: string,
    signal?: AbortSignal,
  ): Promise<LauncherProfileStatus>;
  stopLocalServer(
    profileId: string,
    expectedServerInstanceId: string,
    timeoutSeconds?: number,
  ): Promise<LauncherProfileStatus>;
  restartLocalServer(
    profileId: string,
    expectedServerInstanceId: string,
    timeoutSeconds?: number,
  ): Promise<LauncherProfileStatus>;
  getSshConfig(signal?: AbortSignal): Promise<SshConfigSnapshot>;
  saveSshConfig(text: string): Promise<SshConfigSnapshot>;
  listSshHosts(signal?: AbortSignal): Promise<SshHostsResponse>;
  connectSshHost(
    hostAlias: string,
    rebindServerInstanceId?: string,
  ): Promise<SshHostSessionResponse>;
  disconnectSshHost(hostAlias: string): Promise<SshHostSessionResponse>;
  getSshHostStatus(
    hostAlias: string,
    signal?: AbortSignal,
  ): Promise<SshHostSessionResponse>;
}>;

export function createLauncherApi(
  profile: ProfileContext,
  pageHref: string,
): LauncherApi {
  const origin = new URL(profile.apiBase, pageHref).origin;
  const client = createApiClient(`${origin}/client/v1`, null);
  const lifecycleRequest = async (
    operation: 'stop' | 'restart',
    profileId: string,
    expectedServerInstanceId: string,
    timeoutSeconds = 30,
  ): Promise<LauncherProfileStatus> => {
    const response = await client.post<LauncherProfileStatus>(
      `/profiles/${encodeURIComponent(profileId)}/server/${operation}`,
      {
        expected_server_instance_id: expectedServerInstanceId,
        timeout_seconds: timeoutSeconds,
      },
    );
    return response.data;
  };
  return {
    async getProfileStatus(profileId, signal) {
      const response = await client.get<LauncherProfileStatus>(
        `/profiles/${encodeURIComponent(profileId)}/status`,
        { signal, timeout: 5000 },
      );
      return response.data;
    },
    async connectProfile(profileId, signal) {
      const response = await client.post<LauncherProfileStatus>(
        `/profiles/${encodeURIComponent(profileId)}/connect`,
        {},
        { signal },
      );
      return response.data;
    },
    stopLocalServer(profileId, expectedServerInstanceId, timeoutSeconds) {
      return lifecycleRequest(
        'stop',
        profileId,
        expectedServerInstanceId,
        timeoutSeconds,
      );
    },
    restartLocalServer(profileId, expectedServerInstanceId, timeoutSeconds) {
      return lifecycleRequest(
        'restart',
        profileId,
        expectedServerInstanceId,
        timeoutSeconds,
      );
    },
    async getSshConfig(signal) {
      const response = await client.get<SshConfigSnapshot>(
        '/ssh/config',
        { signal, timeout: 5000 },
      );
      return response.data;
    },
    async saveSshConfig(text) {
      const response = await client.put<SshConfigSnapshot>(
        '/ssh/config',
        { text },
      );
      return response.data;
    },
    async listSshHosts(signal) {
      const response = await client.get<SshHostsResponse>(
        '/ssh/hosts',
        { signal, timeout: 5000 },
      );
      return response.data;
    },
    async connectSshHost(hostAlias, rebindServerInstanceId) {
      const response = await client.post<SshHostSessionResponse>(
        `/ssh/hosts/${encodeURIComponent(hostAlias)}/connect`,
        rebindServerInstanceId
          ? { rebind: true, expected_server_instance_id: rebindServerInstanceId }
          : {},
      );
      return response.data;
    },
    async disconnectSshHost(hostAlias) {
      const response = await client.post<SshHostSessionResponse>(
        `/ssh/hosts/${encodeURIComponent(hostAlias)}/disconnect`,
        {},
      );
      return response.data;
    },
    async getSshHostStatus(hostAlias, signal) {
      const response = await client.get<SshHostSessionResponse>(
        `/ssh/hosts/${encodeURIComponent(hostAlias)}/status`,
        { signal, timeout: 5000 },
      );
      return response.data;
    },
  };
}

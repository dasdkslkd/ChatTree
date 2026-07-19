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

export type LauncherApi = Readonly<{
  getProfileStatus(
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
  };
}

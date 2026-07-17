import type { FrontendBootstrap } from '../runtime/frontendBootstrap';
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
}>;

export function createLauncherApi(
  bootstrap: FrontendBootstrap,
  pageHref: string,
): LauncherApi {
  const origin = new URL(bootstrap.apiBase, pageHref).origin;
  const client = createApiClient(`${origin}/client/v1`, null);
  return {
    async getProfileStatus(profileId, signal) {
      const response = await client.get<LauncherProfileStatus>(
        `/profiles/${encodeURIComponent(profileId)}/status`,
        { signal, timeout: 5000 },
      );
      return response.data;
    },
  };
}

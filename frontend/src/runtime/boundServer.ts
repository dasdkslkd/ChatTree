import type { LauncherProfileStatus } from '../api/launcher';
import type { HandshakeResponse, LeaseGuarded } from '../api/server';
import {
  BoundServerIdentityError,
  BoundServerLeaseChangedError,
  BoundServerNotReadyError,
  BoundServerProtocolError,
  BoundServerStatusError,
  MIN_PROTOCOL_VERSION,
  isCanonicalUuid,
  type BoundServerContext,
} from './connectionIdentity';
import type { ProfileContext } from './profileContext';

export const REQUIRED_SERVER_FEATURES = Object.freeze([
  'runs',
  'sse_replay',
  'error_envelope_v1',
  'idempotent_run_start_v1',
  'cooperative_shutdown_v1',
]);

export type BoundServerProbeDependencies = Readonly<{
  getStatus(signal?: AbortSignal): Promise<LauncherProfileStatus>;
  connect(signal?: AbortSignal): Promise<LauncherProfileStatus>;
  getHandshake(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>>;
}>;

export async function probeBoundServerContext(
  deps: BoundServerProbeDependencies,
  profile: ProfileContext,
  signal?: AbortSignal,
): Promise<BoundServerContext> {
  let status = await deps.getStatus(signal);
  if (status.profile_id !== profile.profileId) {
    throw new BoundServerIdentityError('Launcher returned a different Profile');
  }
  if (status.status === 'error' && status.error && !status.error.retryable) {
    throw new BoundServerStatusError(
      status.error.code,
      status.error.message,
      status.error.retryable,
      status.error.details,
    );
  }
  if (status.status !== 'ready') {
    status = await deps.connect(signal);
    if (status.profile_id !== profile.profileId) {
      throw new BoundServerIdentityError('Launcher connected a different Profile');
    }
  }
  if (status.status === 'error' && status.error) {
    throw new BoundServerStatusError(
      status.error.code,
      status.error.message,
      status.error.retryable,
      status.error.details,
    );
  }
  if (
    status.status !== 'ready'
    || !status.server_instance_id
    || !Number.isInteger(status.connection_epoch)
    || status.connection_epoch < 1
    || !isCanonicalUuid(status.connection_lease_id)
  ) {
    throw new BoundServerNotReadyError('Launcher Profile is not ready');
  }

  const handshake = await deps.getHandshake(status.connection_lease_id, signal);
  if (handshake.connectionLeaseId !== status.connection_lease_id) {
    throw new BoundServerLeaseChangedError(
      'Launcher connection changed during handshake',
    );
  }
  if (
    !Number.isInteger(handshake.data.protocol_version)
    || handshake.data.protocol_version < MIN_PROTOCOL_VERSION
  ) {
    throw new BoundServerProtocolError(
      `ChatTree Server 需要升级，最低协议版本为 ${MIN_PROTOCOL_VERSION}`,
    );
  }
  if (handshake.data.server_instance_id !== status.server_instance_id) {
    throw new BoundServerIdentityError('Server identity mismatch');
  }
  const missingFeatures = REQUIRED_SERVER_FEATURES.filter(
    (feature) => !handshake.data.features.includes(feature),
  );
  if (missingFeatures.length > 0) {
    throw new BoundServerProtocolError(
      `ChatTree Server 需要升级，缺少功能: ${missingFeatures.join(', ')}`,
    );
  }

  return Object.freeze({
    profileId: profile.profileId,
    apiBase: profile.apiBase,
    serverInstanceId: status.server_instance_id,
    connectionEpoch: status.connection_epoch,
    connectionLeaseId: status.connection_lease_id,
  });
}

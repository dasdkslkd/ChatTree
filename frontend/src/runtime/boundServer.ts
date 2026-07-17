import type { LauncherProfileStatus } from '../api/launcher';
import type {
  HandshakeResponse,
  HealthResponse,
  LeaseGuarded,
} from '../api/server';
import {
  BoundServerIdentityError,
  BoundServerLeaseChangedError,
  BoundServerNotReadyError,
  BoundServerProtocolError,
  EXPECTED_PROTOCOL_VERSION,
  isCanonicalUuid,
  type BoundServerContext,
} from './connectionIdentity';
import type { FrontendBootstrap } from './frontendBootstrap';

export type BoundServerProbeDependencies = Readonly<{
  getStatus(signal?: AbortSignal): Promise<LauncherProfileStatus>;
  getHealth(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HealthResponse>>;
  getHandshake(
    expectedLeaseId: string,
    signal?: AbortSignal,
  ): Promise<LeaseGuarded<HandshakeResponse>>;
}>;

export async function probeBoundServerContext(
  deps: BoundServerProbeDependencies,
  bootstrap: FrontendBootstrap,
  signal?: AbortSignal,
): Promise<BoundServerContext> {
  const before = await deps.getStatus(signal);
  if (before.profile_id !== bootstrap.profileId) {
    throw new BoundServerIdentityError('Launcher returned a different Profile');
  }
  if (
    before.status !== 'ready'
    || !before.server_instance_id
    || !Number.isInteger(before.connection_epoch)
    || before.connection_epoch < 1
    || !isCanonicalUuid(before.connection_lease_id)
  ) {
    throw new BoundServerNotReadyError('Launcher Profile is not ready');
  }

  const [health, handshake] = await Promise.all([
    deps.getHealth(before.connection_lease_id, signal),
    deps.getHandshake(before.connection_lease_id, signal),
  ]);
  if (
    health.connectionLeaseId !== before.connection_lease_id
    || handshake.connectionLeaseId !== before.connection_lease_id
  ) {
    throw new BoundServerLeaseChangedError(
      'Launcher connection changed during identity probe',
    );
  }

  const after = await deps.getStatus(signal);
  if (after.profile_id !== bootstrap.profileId) {
    throw new BoundServerIdentityError('Launcher returned a different Profile');
  }
  if (
    after.status !== 'ready'
    || after.connection_epoch !== before.connection_epoch
    || after.connection_lease_id !== before.connection_lease_id
    || after.server_instance_id !== before.server_instance_id
  ) {
    throw new BoundServerLeaseChangedError(
      'Launcher connection changed during handshake',
    );
  }

  if (handshake.data.protocol_version !== EXPECTED_PROTOCOL_VERSION) {
    throw new BoundServerProtocolError(
      `Unsupported ChatTree protocol version: ${handshake.data.protocol_version}`,
    );
  }
  if (
    health.data.server_instance_id !== before.server_instance_id
    || handshake.data.server_instance_id !== before.server_instance_id
  ) {
    throw new BoundServerIdentityError('Server identity mismatch');
  }

  return Object.freeze({
    profileId: bootstrap.profileId,
    apiBase: bootstrap.apiBase,
    serverInstanceId: before.server_instance_id,
    connectionEpoch: before.connection_epoch,
    connectionLeaseId: before.connection_lease_id,
  });
}

import { lazy, Suspense, useCallback, useEffect, useRef } from 'react';

import { BoundServerProvider, useBoundServer } from './runtime/BoundServerProvider';
import {
  connectionScopeRuntime,
  type ConnectionScope,
} from './runtime/connectionScope';
import type { BoundServerContext } from './runtime/connectionIdentity';
import type { ProfileContext } from './runtime/profileContext';
import {
  ALL_PROFILE_STORAGE_KEYS,
  ProfileStorageUnavailableError,
  SERVER_BOUND_PROFILE_STORAGE_KEYS,
  migrateLegacyProfileStorage,
  prepareProfileStorageForServer,
} from './runtime/profileStorage';

const ServerSessionApp = lazy(() => import('./runtime/ServerSessionApp'));

function prepareBoundProfileStorage(context: BoundServerContext): void {
  try {
    migrateLegacyProfileStorage(
      window.localStorage,
      context.profileId,
      ALL_PROFILE_STORAGE_KEYS,
    );
    prepareProfileStorageForServer(
      window.localStorage,
      context.profileId,
      context.serverInstanceId,
      SERVER_BOUND_PROFILE_STORAGE_KEYS,
    );
  } catch (cause) {
    if (cause instanceof ProfileStorageUnavailableError) throw cause;
    throw new ProfileStorageUnavailableError(
      'Profile storage is unavailable',
      { cause },
    );
  }
}

export default function App({ profile }: { profile: ProfileContext }) {
  const reloadStarted = useRef(false);
  const installedScope = useRef<ConnectionScope | null>(null);
  const reloadCurrentPage = useCallback(() => {
    if (reloadStarted.current) return;
    reloadStarted.current = true;
    if (installedScope.current) {
      connectionScopeRuntime.invalidate(installedScope.current);
    }
    window.location.reload();
  }, []);
  const handleInitialContext = useCallback((context: BoundServerContext) => {
    installedScope.current = connectionScopeRuntime.install(context);
  }, []);

  useEffect(
    () => connectionScopeRuntime.subscribeInvalidation(reloadCurrentPage),
    [reloadCurrentPage],
  );

  return (
    <BoundServerProvider
      profile={profile}
      onInitialContext={handleInitialContext}
    >
      <BoundApp />
    </BoundServerProvider>
  );
}

function BoundApp() {
  const state = useBoundServer();
  if (state.status === 'error') {
    const message = state.error instanceof Error
      ? state.error.message
      : 'Server binding failed';
    return <main role="alert" className="startup-error">{message}</main>;
  }
  if (!state.context) {
    return <main className="startup-status">正在连接当前 Server</main>;
  }
  try {
    prepareBoundProfileStorage(state.context);
  } catch (error) {
    const message = error instanceof Error
      ? error.message
      : 'Profile storage is unavailable';
    return <main role="alert" className="startup-error">{message}</main>;
  }
  return (
    <Suspense fallback={<main className="startup-status">正在加载当前 Server</main>}>
      <ServerSessionApp
        binding={state.context}
        connected={state.status === 'ready'}
      />
    </Suspense>
  );
}

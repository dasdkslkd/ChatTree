import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { BoundServerProvider, useBoundServer } from './runtime/BoundServerProvider';
import {
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from './runtime/connectionEpoch';
import type { BoundServerContext } from './runtime/connectionIdentity';
import type { FrontendBootstrap } from './runtime/frontendBootstrap';
import { acquireProfileRendererOwnership } from './runtime/profileRendererOwnership';
import {
  ALL_PROFILE_STORAGE_KEYS,
  ProfileStorageUnavailableError,
  SERVER_BOUND_PROFILE_STORAGE_KEYS,
  migrateLegacyProfileStorage,
  prepareProfileStorageForServer,
} from './runtime/profileStorage';

const ServerSessionApp = lazy(() => import('./runtime/ServerSessionApp'));
const reloadBrowserPage = () => window.location.reload();

function prepareBoundProfileStorage(context: BoundServerContext): void {
  try {
    const storage = window.localStorage;
    migrateLegacyProfileStorage(
      storage,
      context.profileId,
      ALL_PROFILE_STORAGE_KEYS,
    );
    prepareProfileStorageForServer(
      storage,
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

export default function App({ bootstrap }: { bootstrap: FrontendBootstrap }) {
  const reloadStarted = useRef(false);
  const installedToken = useRef<ConnectionEpochToken | null>(null);
  const reloadCurrentPage = useCallback(() => {
    if (reloadStarted.current) return;
    reloadStarted.current = true;
    if (installedToken.current) {
      connectionEpochRuntime.invalidate(installedToken.current);
    }
    reloadBrowserPage();
  }, []);
  const handleInitialContext = useCallback((context: BoundServerContext) => {
    connectionEpochRuntime.install(context);
    installedToken.current = connectionEpochRuntime.capture();
  }, []);

  useEffect(
    () => connectionEpochRuntime.subscribeInvalidation(reloadCurrentPage),
    [reloadCurrentPage],
  );

  return (
    <BoundServerProvider
      bootstrap={bootstrap}
      onInitialContext={handleInitialContext}
      reloadCurrentPage={reloadCurrentPage}
    >
      <BoundApp />
    </BoundServerProvider>
  );
}

function BoundApp() {
  const state = useBoundServer();
  if (state.status === 'error') {
    const message = state.error instanceof Error ? state.error.message : 'Server binding failed';
    return <main role="alert" className="startup-error">{message}</main>;
  }
  if (!state.context) return <main className="startup-status">正在连接当前 Server</main>;
  return (
    <ProfileRendererGate
      context={state.context}
      connected={state.status === 'ready'}
    />
  );
}

type RendererOwnershipState =
  | Readonly<{ status: 'pending'; error: null }>
  | Readonly<{ status: 'ready'; error: null }>
  | Readonly<{ status: 'error'; error: unknown }>;

function ProfileRendererGate({
  context,
  connected,
}: {
  context: BoundServerContext;
  connected: boolean;
}) {
  const [ownership, setOwnership] = useState<RendererOwnershipState>({
    status: 'pending',
    error: null,
  });

  useEffect(() => {
    let mounted = true;
    void acquireProfileRendererOwnership(context.profileId).then(
      () => {
        if (mounted) setOwnership({ status: 'ready', error: null });
      },
      (error) => {
        if (mounted) setOwnership({ status: 'error', error });
      },
    );
    return () => {
      mounted = false;
    };
  }, [context.profileId]);

  if (ownership.status === 'pending') {
    return <main className="startup-status">正在获取 Profile 页面所有权</main>;
  }
  if (ownership.status === 'error') {
    const message = ownership.error instanceof Error
      ? ownership.error.message
      : 'Profile renderer ownership is unavailable';
    return <main role="alert" className="startup-error">{message}</main>;
  }
  try {
    prepareBoundProfileStorage(context);
  } catch (error) {
    const message = error instanceof Error
      ? error.message
      : 'Profile storage is unavailable';
    return <main role="alert" className="startup-error">{message}</main>;
  }
  return (
    <Suspense fallback={<main className="startup-status">正在加载当前 Server</main>}>
      <ServerSessionApp binding={context} connected={connected} />
    </Suspense>
  );
}

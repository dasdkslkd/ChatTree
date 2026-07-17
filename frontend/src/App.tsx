import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
} from 'react';
import { BoundServerProvider, useBoundServer } from './runtime/BoundServerProvider';
import {
  connectionEpochRuntime,
  type ConnectionEpochToken,
} from './runtime/connectionEpoch';
import type { BoundServerContext } from './runtime/connectionIdentity';
import type { FrontendBootstrap } from './runtime/frontendBootstrap';

const ServerSessionApp = lazy(() => import('./runtime/ServerSessionApp'));
const reloadBrowserPage = () => window.location.reload();

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
    <Suspense fallback={<main className="startup-status">正在加载当前 Server</main>}>
      <ServerSessionApp binding={state.context} connected={state.status === 'ready'} />
    </Suspense>
  );
}

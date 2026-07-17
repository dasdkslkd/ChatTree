import { lazy, Suspense } from 'react';
import { BoundServerProvider, useBoundServer } from './runtime/BoundServerProvider';
import type { FrontendBootstrap } from './runtime/frontendBootstrap';

const ServerSessionApp = lazy(() => import('./runtime/ServerSessionApp'));

export default function App({ bootstrap }: { bootstrap: FrontendBootstrap }) {
  return (
    <BoundServerProvider bootstrap={bootstrap}>
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

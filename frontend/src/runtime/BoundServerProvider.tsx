import {
  createContext,
  type PropsWithChildren,
  useContext,
  useEffect,
  useReducer,
} from 'react';

import { createApiClient } from '../api/client';
import { createLauncherApi } from '../api/launcher';
import { createServerApi } from '../api/server';
import { createInitialBindingState, reduceBindingState, type BindingState } from './bindingState';
import { probeBoundServerContext } from './boundServer';
import { BoundServerProbeOwner } from './boundServerProbeOwner';
import type { BoundServerContext } from './connectionIdentity';
import type { FrontendBootstrap } from './frontendBootstrap';

const BoundServerStateContext = createContext<BindingState | null>(null);
const defaultReloadCurrentPage = () => window.location.reload();

export function BoundServerProvider({
  bootstrap,
  onInitialContext,
  reloadCurrentPage = defaultReloadCurrentPage,
  children,
}: PropsWithChildren<{
  bootstrap: FrontendBootstrap;
  onInitialContext?(context: BoundServerContext): void;
  reloadCurrentPage?(): void;
}>) {
  const [state, dispatch] = useReducer(
    reduceBindingState,
    undefined,
    createInitialBindingState,
  );

  useEffect(() => {
    const launcher = createLauncherApi(bootstrap, window.location.href);
    const server = createServerApi(createApiClient(bootstrap.apiBase));
    const owner = new BoundServerProbeOwner({
      probe: (signal) => probeBoundServerContext({
        getStatus: (requestSignal) => launcher.getProfileStatus(
          bootstrap.profileId,
          requestSignal,
        ),
        getHealth: server.health,
        getHandshake: server.handshake,
      }, bootstrap, signal),
      dispatch,
      onInitialContext,
      reloadCurrentPage,
      scheduler: window,
    });
    owner.start();
    return () => owner.dispose();
  }, [bootstrap, onInitialContext, reloadCurrentPage]);

  return (
    <BoundServerStateContext.Provider value={state}>
      {children}
    </BoundServerStateContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBoundServer(): BindingState {
  const state = useContext(BoundServerStateContext);
  if (!state) {
    throw new Error('useBoundServer must be used inside BoundServerProvider');
  }
  return state;
}

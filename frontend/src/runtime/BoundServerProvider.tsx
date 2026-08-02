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
import {
  createInitialBindingState,
  reduceBindingState,
  type BindingState,
} from './bindingState';
import { probeBoundServerContext } from './boundServer';
import {
  isFatalBoundServerError,
  type BoundServerContext,
} from './connectionIdentity';
import type { ProfileContext } from './profileContext';

const BoundServerStateContext = createContext<BindingState | null>(null);
const RETRY_DELAYS_MS = [500, 1000, 2000, 5000] as const;

function wait(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, delay);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });
}

export function BoundServerProvider({
  profile,
  onInitialContext,
  children,
}: PropsWithChildren<{
  profile: ProfileContext;
  onInitialContext?(context: BoundServerContext): void;
}>) {
  const [state, dispatch] = useReducer(
    reduceBindingState,
    undefined,
    createInitialBindingState,
  );

  useEffect(() => {
    const controller = new AbortController();
    const launcher = createLauncherApi(profile, window.location.href);
    const server = createServerApi(createApiClient(profile.apiBase, null));
    void (async () => {
      let retry = 0;
      while (!controller.signal.aborted) {
        try {
          const context = await probeBoundServerContext({
            getStatus: (signal) => launcher.getProfileStatus(
              profile.profileId,
              signal,
            ),
            connect: (signal) => launcher.connectProfile(
              profile.profileId,
              signal,
            ),
            getHandshake: server.handshake,
          }, profile, controller.signal);
          onInitialContext?.(context);
          dispatch({ type: 'probe_ready', context });
          return;
        } catch (error) {
          if (controller.signal.aborted) return;
          if (isFatalBoundServerError(error)) {
            dispatch({ type: 'fatal_error', error });
            return;
          }
          dispatch({ type: 'probe_failed', error });
          const delay = RETRY_DELAYS_MS[Math.min(retry, RETRY_DELAYS_MS.length - 1)];
          retry += 1;
          try {
            await wait(delay, controller.signal);
          } catch {
            return;
          }
        }
      }
    })();
    return () => controller.abort();
  }, [profile, onInitialContext]);

  return (
    <BoundServerStateContext.Provider value={state}>
      {children}
    </BoundServerStateContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useBoundServer(): BindingState {
  const state = useContext(BoundServerStateContext);
  if (!state) throw new Error('useBoundServer must be used inside BoundServerProvider');
  return state;
}

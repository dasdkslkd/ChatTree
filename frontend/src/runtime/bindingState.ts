import {
  BoundServerIdentityError,
  BoundServerLeaseChangedError,
  sameBoundServerContext,
  type BoundServerContext,
} from './connectionIdentity';

export type BindingState = Readonly<{
  status: 'connecting' | 'ready' | 'disconnected' | 'error';
  context: BoundServerContext | null;
  error: unknown | null;
}>;

export type BindingEvent =
  | { type: 'probe_ready'; context: BoundServerContext }
  | { type: 'probe_failed'; error: unknown }
  | { type: 'fatal_error'; error: unknown };

export function createInitialBindingState(): BindingState {
  return { status: 'connecting', context: null, error: null };
}

export function reduceBindingState(
  state: BindingState,
  event: BindingEvent,
): BindingState {
  if (event.type === 'fatal_error') {
    return { ...state, status: 'error', error: event.error };
  }
  if (event.type === 'probe_failed') {
    return {
      ...state,
      status: state.context ? 'disconnected' : 'connecting',
      error: event.error,
    };
  }
  if (state.context && (
    state.context.profileId !== event.context.profileId
    || state.context.apiBase !== event.context.apiBase
  )) {
    throw new BoundServerIdentityError(
      'Bound frontend cannot change Profile or API base',
    );
  }
  if (state.context && !sameBoundServerContext(state.context, event.context)) {
    throw new BoundServerLeaseChangedError(
      'Bound frontend must reload for a new Server generation',
    );
  }
  return {
    status: 'ready',
    context: state.context ?? event.context,
    error: null,
  };
}

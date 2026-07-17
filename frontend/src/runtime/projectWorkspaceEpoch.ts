import type { ConnectionEpochRuntime, ConnectionEpochToken } from './connectionEpoch';

type ProjectWorkspaceEpochSource = Pick<ConnectionEpochRuntime, 'isCurrent'>;

type ProjectWorkspaceCallbacks<T> = {
  resolve: () => Promise<T>;
  onSuccess: (value: T) => void;
  onError: (error: unknown) => void;
  onFinally: () => void;
};

export async function resolveProjectWorkspaceForEpoch<T>(
  token: ConnectionEpochToken,
  callbacks: ProjectWorkspaceCallbacks<T>,
  epochSource: ProjectWorkspaceEpochSource,
): Promise<T | null> {
  try {
    const value = await callbacks.resolve();
    if (!epochSource.isCurrent(token)) return null;
    callbacks.onSuccess(value);
    return value;
  } catch (error: unknown) {
    if (!epochSource.isCurrent(token)) return null;
    callbacks.onError(error);
    return null;
  } finally {
    if (epochSource.isCurrent(token)) callbacks.onFinally();
  }
}

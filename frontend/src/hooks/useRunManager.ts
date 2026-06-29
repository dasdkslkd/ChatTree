import { useCallback, useSyncExternalStore } from 'react';
import { streamManager, type StreamState } from '../services/streamManager';

const EMPTY_RUN_STATES: StreamState[] = [];

export function useRunManager(conversationId: string | null): StreamState[] {
  return useSyncExternalStore(
    useCallback((callback: () => void) => {
      if (!conversationId) return () => {};
      return streamManager.subscribe((changedId) => {
        if (changedId === conversationId) callback();
      });
    }, [conversationId]),
    useCallback(() => {
      if (!conversationId) return EMPTY_RUN_STATES;
      return streamManager.getConversationStates(conversationId);
    }, [conversationId])
  );
}

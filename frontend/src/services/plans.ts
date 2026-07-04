import { apiClient } from '../api/client';
import type { PlanSession } from '../types/plan';

type PlansApiClient = {
  get: (url: string, config?: any) => Promise<{ data: unknown }>;
  post: (url: string, payload?: any) => Promise<{ data: unknown }>;
};

function asPlanSession(data: unknown): PlanSession | null {
  if (!data || typeof data !== 'object') return null;
  const envelope = data as { plan?: unknown };
  const candidate = (envelope.plan && typeof envelope.plan === 'object' ? envelope.plan : data) as Partial<PlanSession>;
  const planId = candidate.plan_id || candidate.id;
  if (typeof planId !== 'string' || typeof candidate.status !== 'string') return null;
  return { ...candidate, id: candidate.id || planId, plan_id: planId } as PlanSession;
}

export function createPlansService(client: PlansApiClient) {
  return {
    fetchActive: async (conversationId: string): Promise<PlanSession | null> => {
      const encodedConversationId = encodeURIComponent(conversationId);
      const response = await client.get(`/conversations/${encodedConversationId}/plans/current`);
      return asPlanSession(response.data);
    },

    approve: async (conversationId: string, planId: string): Promise<PlanSession | null> => {
      const response = await client.post(
        `/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/approve`,
        {},
      );
      return asPlanSession(response.data);
    },

    reject: async (conversationId: string, planId: string, feedback: string): Promise<PlanSession | null> => {
      const response = await client.post(
        `/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/reject`,
        { feedback },
      );
      return asPlanSession(response.data);
    },

    answer: async (conversationId: string, planId: string, answer: string): Promise<PlanSession | null> => {
      const response = await client.post(
        `/conversations/${encodeURIComponent(conversationId)}/plans/${encodeURIComponent(planId)}/answer`,
        { answer },
      );
      return asPlanSession(response.data);
    },
  };
}

export const plansService = createPlansService(apiClient);

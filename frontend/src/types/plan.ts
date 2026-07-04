export type PlanSessionStatus =
  | 'idle'
  | 'drafting'
  | 'active'
  | 'awaiting_question'
  | 'awaiting_approval'
  | 'approved'
  | 'rejected'
  | 'implementing'
  | 'completed'
  | 'cancelled';

export interface PlanSession {
  id?: string;
  plan_id: string;
  conversation_id: string;
  node_id?: string | null;
  status: PlanSessionStatus;
  plan?: string | null;
  plan_markdown?: string | null;
  markdown?: string | null;
  content?: string | null;
  feedback?: string | null;
  question?: {
    question?: string | null;
    options?: Array<{
      label?: string | null;
      description?: string | null;
    }> | null;
    answer?: string | null;
  } | null;
  previous_permission_mode?: string | null;
  created_at?: number | string | null;
  updated_at?: number | string | null;
}

export interface RejectPlanRequest {
  feedback: string;
}

export interface AnswerPlanQuestionRequest {
  answer: string;
}

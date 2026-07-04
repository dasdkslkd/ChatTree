import type { PlanSession } from '../types/plan';

export function getPlanApprovalMarkdown(plan: Pick<PlanSession, 'plan' | 'plan_markdown' | 'markdown' | 'content'> | null | undefined): string {
  const value = plan?.plan || plan?.plan_markdown || plan?.markdown || plan?.content || '';
  return value.trim();
}

export function shouldShowPlanApproval(plan: Pick<PlanSession, 'status' | 'plan' | 'plan_markdown' | 'markdown' | 'content'> | null | undefined): boolean {
  return plan?.status === 'awaiting_approval' && getPlanApprovalMarkdown(plan).length > 0;
}

export function shouldShowPlanSummary(plan: Pick<PlanSession, 'status' | 'plan' | 'plan_markdown' | 'markdown' | 'content'> | null | undefined): boolean {
  return plan?.status === 'approved' && getPlanApprovalMarkdown(plan).length > 0;
}

export function getPlanQuestionText(plan: Pick<PlanSession, 'question'> | null | undefined): string {
  const value = plan?.question?.question || '';
  return value.trim();
}

export function shouldShowPlanQuestion(plan: Pick<PlanSession, 'status' | 'question'> | null | undefined): boolean {
  return plan?.status === 'awaiting_question' && getPlanQuestionText(plan).length > 0;
}

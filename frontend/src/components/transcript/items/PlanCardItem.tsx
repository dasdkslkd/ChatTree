import { useEffect, useState } from 'react';
import { Check, ClipboardList, Loader2, MessageSquare, X } from 'lucide-react';
import MarkdownContent from '../../MarkdownContent';
import type {
  TranscriptItem,
  TranscriptPlanActionHandler,
  TranscriptPlanQuestionAnswerHandler,
} from '../../../types/transcript';
import { getItemText, getStatusText } from './itemText';

interface PlanCardItemProps {
  item: TranscriptItem;
  onApprovePlan?: TranscriptPlanActionHandler;
  onRejectPlan?: TranscriptPlanActionHandler;
  onAnswerPlanQuestion?: TranscriptPlanQuestionAnswerHandler;
  planActionPending?: string | null;
  planError?: string | null;
}

type PlanQuestionOption = {
  label?: string | null;
  description?: string | null;
};

type PlanQuestionPayload = {
  question?: string | null;
  options?: PlanQuestionOption[] | null;
};

function getPlanQuestionPayload(item: TranscriptItem): PlanQuestionPayload | null {
  const value = item.props?.question;
  if (!value || typeof value !== 'object') return null;
  return value as PlanQuestionPayload;
}

function getPlanQuestionText(item: TranscriptItem): string {
  const question = getPlanQuestionPayload(item)?.question;
  if (typeof question === 'string' && question.trim()) return question.trim();
  return getItemText(item, '').trim();
}

function getPlanQuestionOptions(item: TranscriptItem): PlanQuestionOption[] {
  const options = getPlanQuestionPayload(item)?.options;
  if (!Array.isArray(options)) return [];
  return options.filter((option) => typeof option?.label === 'string' && option.label.trim().length > 0);
}

export function PlanCardItem({
  item,
  onApprovePlan,
  onRejectPlan,
  onAnswerPlanQuestion,
  planActionPending = null,
  planError = null,
}: PlanCardItemProps) {
  const text = getItemText(item, 'Plan update');
  const status = getStatusText(item);
  const isAwaitingApproval = status === 'awaiting_approval';
  const question = getPlanQuestionText(item);
  const isAwaitingQuestion = status === 'awaiting_question' && question.length > 0;
  const options = isAwaitingQuestion ? getPlanQuestionOptions(item) : [];
  const [draftAnswer, setDraftAnswer] = useState('');
  const answering = planActionPending === 'answer';

  useEffect(() => {
    setDraftAnswer('');
  }, [item.id, status]);

  const submitAnswer = async () => {
    const answer = draftAnswer.trim();
    if (!answer) return;
    await onAnswerPlanQuestion?.(item, answer);
    setDraftAnswer('');
  };

  return (
    <div className="transcript-plan-card w-full my-2 flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-1.5 text-sm"
        style={{
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <ClipboardList className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>Plan{status ? ` · ${status}` : ''}</span>
        </div>
        {!isAwaitingQuestion && (
          <div
            className="min-w-0 prose prose-sm max-w-none [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
            style={{
              color: 'var(--fg-secondary)',
              fontSize: 'var(--codex-chat-font-size)',
              lineHeight: 'calc(var(--codex-chat-font-size) + 8px)',
            }}
          >
            <MarkdownContent enableMermaid>{text}</MarkdownContent>
          </div>
        )}
        {isAwaitingApproval && (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-primary)',
              }}
              onClick={() => onApprovePlan?.(item)}
            >
              <Check className="h-3.5 w-3.5" />
              批准
            </button>
            <button
              type="button"
              className="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors"
              style={{
                borderColor: 'var(--border)',
                background: 'var(--bg-button-tertiary)',
                color: 'var(--fg-secondary)',
              }}
              onClick={() => onRejectPlan?.(item)}
            >
              <X className="h-3.5 w-3.5" />
              要求修改
            </button>
          </div>
        )}
        {isAwaitingQuestion && (
          <div
            className="plan-question-card flex w-full min-w-0 flex-col gap-3 rounded-md px-3 py-3 text-sm"
            style={{
              border: '0.5px solid var(--border)',
              background: 'var(--bg-secondary)',
              color: 'var(--fg-secondary)',
            }}
          >
            <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
              <MessageSquare className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
              <span>计划澄清</span>
            </div>
            <div className="text-sm leading-6" style={{ color: 'var(--fg-primary)' }}>{question}</div>
            {options.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {options.map((option, index) => {
                  const label = (option.label || '').trim();
                  return (
                    <button
                      key={`${label}-${index}`}
                      type="button"
                      className="inline-flex min-h-7 max-w-full items-center justify-start gap-1 rounded-md border px-2 py-1 text-left text-xs transition-colors"
                      style={{
                        borderColor: 'var(--border)',
                        background: 'var(--bg-button-secondary)',
                        color: 'var(--fg-primary)',
                      }}
                      onClick={() => setDraftAnswer(label)}
                      disabled={planActionPending !== null}
                    >
                      {draftAnswer.trim() === label ? <Check className="h-3.5 w-3.5 shrink-0" /> : <span className="h-3.5 w-3.5 shrink-0" />}
                      <span className="min-w-0 truncate">{label}</span>
                    </button>
                  );
                })}
              </div>
            )}
            <textarea
              value={draftAnswer}
              onChange={(event) => setDraftAnswer(event.target.value)}
              placeholder="输入回答"
              className="min-h-[64px] w-full resize-none rounded-md px-2.5 py-2 text-sm outline-none"
              style={{
                border: '0.5px solid var(--border)',
                background: 'var(--bg-input)',
                color: 'var(--fg-primary)',
              }}
            />
            {planError && (
              <div className="text-xs" style={{ color: 'var(--destructive)' }}>{planError}</div>
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs transition-colors"
                style={{
                  background: 'var(--bg-button-primary)',
                  color: 'var(--fg-on-primary)',
                }}
                onClick={() => {
                  void submitAnswer();
                }}
                disabled={planActionPending !== null || !draftAnswer.trim()}
              >
                {answering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                提交回答
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

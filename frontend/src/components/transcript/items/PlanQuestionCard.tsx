import { useState } from 'react';
import { Check, Loader2, MessageSquare } from 'lucide-react';
import type { PlanQuestionItem, TranscriptPlanQuestionAnswerHandler } from '../../../types/transcript';

export function PlanQuestionCard({
  item,
  onAnswerPlanQuestion,
  planActionPending = null,
  planErrorByItem = {},
}: {
  item: PlanQuestionItem;
  onAnswerPlanQuestion?: TranscriptPlanQuestionAnswerHandler;
  planActionPending?: string | null;
  planErrorByItem?: Record<string, string>;
}) {
  const questions = Array.isArray(item.questions) ? item.questions : [];
  const [drafts, setDrafts] = useState<string[]>(() => questions.map(() => ''));
  const answered = item.status === 'answered';
  const answering = planActionPending === 'answer';
  const allAnswered = drafts.length > 0 && drafts.every((draft) => draft.trim().length > 0);
  const answers = item.answers ?? [];

  const setDraft = (index: number, value: string) => {
    setDrafts((prev) => prev.map((draft, i) => (i === index ? value : draft)));
  };

  const submit = async () => {
    if (!allAnswered) return;
    await onAnswerPlanQuestion?.(item, drafts.map((draft) => draft.trim()));
    setDrafts(questions.map(() => ''));
  };

  return (
    <div className="transcript-plan-question w-full flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-3 rounded-md px-3 py-3 text-sm"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-button-secondary)',
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <MessageSquare className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>计划澄清 · {answered ? '已回答' : '等待回答'}</span>
        </div>
        {questions.map((entry, index) => {
          const options = Array.isArray(entry.options) ? entry.options : [];
          return (
            <div key={index} className="flex w-full flex-col gap-2">
              <div className="text-sm leading-6" style={{ color: 'var(--fg-85)' }}>
                {questions.length > 1 ? `${index + 1}. ` : ''}{entry.question || ''}
              </div>
              {answered && answers[index] != null && (
                <div
                  className="rounded-md px-2.5 py-2 text-sm"
                  style={{
                    border: '0.5px solid var(--border)',
                    background: 'var(--bg-input)',
                    color: 'var(--fg-85)',
                  }}
                >
                  {answers[index]}
                </div>
              )}
              {!answered && options.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {options.map((option, optionIndex) => {
                    const label = (option.label || '').trim();
                    if (!label) return null;
                    return (
                      <button
                        key={`${label}-${optionIndex}`}
                        type="button"
                        className="inline-flex min-h-7 max-w-full items-center justify-start gap-1 rounded-md border px-2 py-1 text-left text-xs transition-colors"
                        style={{
                          borderColor: 'var(--border)',
                          background: 'var(--bg-button-secondary)',
                          color: 'var(--fg-85)',
                        }}
                        onClick={() => setDraft(index, label)}
                        disabled={planActionPending !== null}
                      >
                        {drafts[index].trim() === label ? <Check className="h-3.5 w-3.5 shrink-0" /> : <span className="h-3.5 w-3.5 shrink-0" />}
                        <span className="min-w-0 truncate">{label}</span>
                      </button>
                    );
                  })}
                </div>
              )}
              {!answered && (
                <textarea
                  value={drafts[index]}
                  onChange={(event) => setDraft(index, event.target.value)}
                  disabled={planActionPending !== null}
                  placeholder="输入回答"
                  className="min-h-[64px] w-full resize-none rounded-md px-2.5 py-2 text-sm outline-none"
                  style={{
                    border: '0.5px solid var(--border)',
                    background: 'var(--bg-input)',
                    color: 'var(--fg-85)',
                  }}
                />
              )}
            </div>
          );
        })}
        {!answered && (
          <>
            {planErrorByItem[item.id] && <div className="text-xs" style={{ color: 'var(--destructive)' }}>{planErrorByItem[item.id]}</div>}
            <button
              type="button"
              className="inline-flex h-7 w-fit items-center gap-1 rounded-md px-2 text-xs transition-colors"
              style={{
                background: 'var(--bg-button-secondary)',
                color: 'var(--fg-85)',
              }}
              onClick={() => { void submit(); }}
              disabled={planActionPending !== null || !allAnswered}
            >
              {answering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              提交全部回答
            </button>
          </>
        )}
      </div>
    </div>
  );
}
import { useState } from 'react';
import { Check, Loader2, MessageSquare } from 'lucide-react';
import type { PlanQuestionItem, TranscriptPlanQuestionAnswerHandler } from '../../../types/transcript';

export function PlanQuestionCard({
  item,
  onAnswerPlanQuestion,
  planActionPending = null,
  planError = null,
}: {
  item: PlanQuestionItem;
  onAnswerPlanQuestion?: TranscriptPlanQuestionAnswerHandler;
  planActionPending?: string | null;
  planError?: string | null;
}) {
  const [draftAnswer, setDraftAnswer] = useState('');
  const answered = item.status === 'answered';
  const options = Array.isArray(item.options) ? item.options : [];
  const answering = planActionPending === 'answer';

  const submit = async () => {
    const answer = draftAnswer.trim();
    if (!answer) return;
    await onAnswerPlanQuestion?.(item, answer);
    setDraftAnswer('');
  };

  return (
    <div className="transcript-plan-question w-full flex flex-col items-start" role="listitem">
      <div
        className="flex max-w-[760px] w-full min-w-0 flex-col gap-3 rounded-md px-3 py-3 text-sm"
        style={{
          border: '0.5px solid var(--border)',
          background: 'var(--bg-secondary)',
          color: 'var(--fg-secondary)',
        }}
      >
        <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
          <MessageSquare className="h-3.5 w-3.5 shrink-0" style={{ color: 'var(--icon-accent)' }} />
          <span>计划澄清 · {answered ? '已回答' : '等待回答'}</span>
        </div>
        <div className="text-sm leading-6" style={{ color: 'var(--fg-primary)' }}>{item.question || ''}</div>
        {answered && item.answer && (
          <div
            className="rounded-md px-2.5 py-2 text-sm"
            style={{
              border: '0.5px solid var(--border)',
              background: 'var(--bg-input)',
              color: 'var(--fg-primary)',
            }}
          >
            {item.answer}
          </div>
        )}
        {!answered && options.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {options.map((option, index) => {
              const label = (option.label || '').trim();
              if (!label) return null;
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
        {!answered && (
          <>
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
            {planError && <div className="text-xs" style={{ color: 'var(--destructive)' }}>{planError}</div>}
            <button
              type="button"
              className="inline-flex h-7 w-fit items-center gap-1 rounded-md px-2 text-xs transition-colors"
              style={{
                background: 'var(--bg-button-primary)',
                color: 'var(--fg-on-primary)',
              }}
              onClick={() => { void submit(); }}
              disabled={planActionPending !== null || !draftAnswer.trim()}
            >
              {answering ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
              提交回答
            </button>
          </>
        )}
      </div>
    </div>
  );
}

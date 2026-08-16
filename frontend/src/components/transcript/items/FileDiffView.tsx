import { useCallback, useEffect, useState } from 'react';
import { diffLines } from 'diff';
import { RotateCcw } from 'lucide-react';
import { toast } from '@/utils/toast';
import { messageApi } from '../../../api/message';

interface FileDiffViewProps {
  path: string;
  toolResultId?: string;
  args: Record<string, unknown>;
}

interface DiffEntry {
  before: string;
  after?: string;
}

function DiffHunk({ before, after }: { before: string; after: string }) {
  const parts = diffLines(before, after);
  let added = 0;
  let removed = 0;
  for (const part of parts) {
    if (part.added) added += part.value.split('\n').filter((line) => line).length;
    if (part.removed) removed += part.value.split('\n').filter((line) => line).length;
  }
  return (
    <div className="tc-diff">
      <div className="tc-diff-meta">
        <span className="tc-diff-add">+{added}</span>
        <span className="tc-diff-del">-{removed}</span>
      </div>
      <div className="tc-diff-body">
        {parts.map((part, index) => {
          const lines = part.value.replace(/\n$/, '').split('\n');
          if (part.added) {
            return lines.map((line, lineIndex) => (
              <div key={`${index}:${lineIndex}`} className="tc-diff-line tc-diff-line-add">
                <span className="tc-diff-mark">+</span>
                <span className="tc-diff-text">{line}</span>
              </div>
            ));
          }
          if (part.removed) {
            return lines.map((line, lineIndex) => (
              <div key={`${index}:${lineIndex}`} className="tc-diff-line tc-diff-line-del">
                <span className="tc-diff-mark">-</span>
                <span className="tc-diff-text">{line}</span>
              </div>
            ));
          }
          return lines.map((line, lineIndex) => (
            <div key={`${index}:${lineIndex}`} className="tc-diff-line tc-diff-line-ctx">
              <span className="tc-diff-mark"> </span>
              <span className="tc-diff-text">{line}</span>
            </div>
          ));
        })}
      </div>
    </div>
  );
}

export function FileDiffView({ path, toolResultId, args }: FileDiffViewProps) {
  const [entry, setEntry] = useState<DiffEntry | null>(null);
  const [loading, setLoading] = useState(Boolean(toolResultId));
  const [loadError, setLoadError] = useState(false);
  const [reverting, setReverting] = useState(false);

  useEffect(() => {
    if (!toolResultId) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoadError(false);
    messageApi
      .getToolResult(toolResultId)
      .then((slice) => {
        if (cancelled) return;
        const before = slice.diff_before;
        const first = before ? Object.entries(before)[0] : null;
        if (first) {
          setEntry({ before: first[1].before, after: first[1].after });
        }
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [toolResultId]);

  const hasAfter = (entry && typeof entry.after === 'string')
    || Object.prototype.hasOwnProperty.call(args, 'content');
  const after = (entry && typeof entry.after === 'string')
    ? entry.after
    : (typeof args.content === 'string' ? args.content : '');

  const handleRevert = useCallback(async () => {
    if (!toolResultId) return;
    if (!window.confirm('确定要回退这个文件的变更吗？')) return;
    setReverting(true);
    try {
      await messageApi.revertToolResult(toolResultId);
      toast.success('已回退变更');
    } catch {
      toast.error('回退失败');
    } finally {
      setReverting(false);
    }
  }, [toolResultId]);

  const revertible = Boolean(toolResultId);

  return (
    <div className="tc-file-diff">
      <MetaRow path={path} revertible={revertible} reverting={reverting} onRevert={handleRevert} />
      {loading && <div className="tc-empty">加载变更中...</div>}
      {!loading && loadError && <div className="tc-empty">加载变更记录失败</div>}
      {!loading && !loadError && !revertible && <div className="tc-empty">该改动发生在本功能启用之前，暂无变更快照可展示</div>}
      {!loading && !loadError && revertible && entry === null && <div className="tc-empty">此改动无可回退的变更记录</div>}
      {!loading && !loadError && entry !== null && !hasAfter && <div className="tc-empty">无法还原完整新内容，可回退该改动</div>}
      {!loading && !loadError && entry !== null && hasAfter && <DiffHunk before={entry.before} after={after} />}
    </div>
  );
}

function MetaRow({
  path,
  revertible,
  reverting,
  onRevert,
}: {
  path: string;
  revertible: boolean;
  reverting: boolean;
  onRevert: () => void;
}) {
  return (
    <div className="tc-diff-toolbar">
      <span className="tc-meta-value">{path}</span>
      {revertible && (
        <button type="button" className="tc-copy tc-copy-subtle" onClick={onRevert} disabled={reverting}>
          <RotateCcw className="h-3 w-3" />
          <span>{reverting ? '回退中...' : '回退改动'}</span>
        </button>
      )}
    </div>
  );
}
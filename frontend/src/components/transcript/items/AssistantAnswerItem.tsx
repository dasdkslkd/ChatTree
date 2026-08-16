import { useMemo, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, Copy, Network, Pencil, RotateCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import MarkdownContent from '../../MarkdownContent';
import { useConversationStore } from '../../../store/conversationStore';
import { useNavigationStore } from '../../../store/navigationStore';
import type { AssistantAnswerItem as AssistantAnswerTranscriptItem, TranscriptCopyHandler } from '../../../types/transcript';
import { getItemText } from './itemText';
import { getStreamStatusText } from '../../../utils/streaming';
import { formatClockTime } from '../../../utils/time';

export function AssistantAnswerItem({
  item,
  onCopy,
  onRetry,
  onEditBranch,
}: {
  item: AssistantAnswerTranscriptItem;
  onCopy?: TranscriptCopyHandler;
  onRetry?: (item: AssistantAnswerTranscriptItem) => void | Promise<void>;
  onEditBranch?: (item: AssistantAnswerTranscriptItem) => void | Promise<void>;
}) {
  const [copied, setCopied] = useState(false);
  // 按 node_id 细粒度订阅树节点：流式 usage patch 重建 treeData 时，只要本节点
  // 与父节点引用未变，组件即不重渲染（selector 返回既有节点对象，引用稳定）
  const treeNode = useConversationStore(
    (state) => state.treeData?.nodes.find((entry) => entry.id === item.node_id) ?? null,
  );
  const parentNode = useConversationStore((state) => {
    const node = state.treeData?.nodes.find((entry) => entry.id === item.node_id);
    return node?.parent_id
      ? state.treeData?.nodes.find((entry) => entry.id === node.parent_id) ?? null
      : null;
  });
  const switchNode = useConversationStore((state) => state.switchNode);
  const setChatViewMode = useNavigationStore((state) => state.setChatViewMode);
  const text = useMemo(() => getItemText(item), [item]);
  const statusLabel = item.status === 'error' && item.finish_reason
    ? `生成未完成：${item.finish_reason}`
    : getStreamStatusText(item.status || '', null);

  // 兄弟分支翻页器：复用树数据 children_ids 与 switchNode，原地切换分支；
  // parentNode.user_content 即该回答对应的用户输入，供重试/编辑分叉使用
  const branchSiblings = parentNode?.children_ids ?? [];
  const branchIndex = branchSiblings.indexOf(item.node_id);

  if (!text) return null;

  const handleCopy = async () => {
    try {
      await onCopy?.(item, text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // onCopy 失败已由调用方 toast，这里保持未复制状态
    }
  };

  const showBranchPager = branchSiblings.length > 1 && branchIndex !== -1;
  const showBranchActions = Boolean(parentNode?.user_content && (onRetry || onEditBranch));
  const metaParts = [treeNode?.model_id, formatClockTime(treeNode?.timestamp)].filter(Boolean);
  const showActionBar = Boolean(onCopy || showBranchPager || showBranchActions || metaParts.length > 0);

  return (
    <div className={cn('w-full flex flex-col group items-start')} role="listitem">
      <div className="flex flex-col max-w-full min-w-0 items-start w-full">
        <div
          className="max-w-full min-w-0 break-words px-3 py-2 rounded-2xl leading-relaxed prose prose-sm [&_p]:m-0 [&_p:not(:last-child)]:mb-2"
          style={{
            color: 'var(--fg-secondary)',
            fontSize: 'var(--codex-chat-font-size)',
            lineHeight: 'calc(var(--codex-chat-font-size) + 9px)',
          }}
        >
          <MarkdownContent enableMermaid>{text}</MarkdownContent>
        </div>
        {showActionBar && (
          <div className="msg-action-bar">
            {onCopy && (
              <button
                type="button"
                className={cn('msg-action-btn', copied && 'is-copied')}
                onClick={handleCopy}
                aria-label="复制消息"
              >
                {copied ? <Check /> : <Copy />}
                {copied ? '已复制' : '复制'}
              </button>
            )}
            {showBranchActions && onRetry && (
              <button
                type="button"
                className="msg-action-btn"
                onClick={() => void onRetry(item)}
                aria-label="重试"
              >
                <RotateCw />重试
              </button>
            )}
            {showBranchActions && onEditBranch && (
              <button
                type="button"
                className="msg-action-btn"
                onClick={() => void onEditBranch(item)}
                aria-label="编辑分叉"
              >
                <Pencil />编辑分叉
              </button>
            )}
            {showBranchPager && (
              <span className="branch-pager">
                <button
                  type="button"
                  className="branch-pager-btn"
                  disabled={branchIndex <= 0}
                  onClick={() => void switchNode(branchSiblings[branchIndex - 1]).catch(() => {})}
                  aria-label="上一分支"
                >
                  <ChevronLeft />
                </button>
                <span className="branch-pager-label">{branchIndex + 1} / {branchSiblings.length}</span>
                <button
                  type="button"
                  className="branch-pager-btn"
                  disabled={branchIndex >= branchSiblings.length - 1}
                  onClick={() => void switchNode(branchSiblings[branchIndex + 1]).catch(() => {})}
                  aria-label="下一分支"
                >
                  <ChevronRight />
                </button>
                <button
                  type="button"
                  className="branch-tree-btn"
                  onClick={() => setChatViewMode('tree')}
                >
                  <Network />在树中查看
                </button>
              </span>
            )}
            {metaParts.length > 0 && (
              <span className="msg-action-meta">{metaParts.join(' · ')}</span>
            )}
          </div>
        )}
        {statusLabel && (
          <div className="mt-1 text-xs text-destructive">
            {statusLabel}
          </div>
        )}
      </div>
    </div>
  );
}

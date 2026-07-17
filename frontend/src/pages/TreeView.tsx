import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import dagre from 'dagre';
import { useConversationStore } from '../store/conversationStore';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { TextTooltip } from '@/components/ui/text-tooltip';
import { Textarea } from '@/components/ui/textarea';
import { ZoomIn, ZoomOut, Maximize2, ArrowDown, ArrowRight, Trash2, Scissors, FileText, Loader2, X, Copy, Check } from 'lucide-react';
import type { PruneSummaryRecord, TreeNode } from '../api/conversation';
import { getTreeUserContent } from '../utils/taskNotificationVisibility';
import { useRunManager } from '../hooks/useRunManager';
import { streamManager } from '../services/streamManager';

interface LayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  data: TreeNode;
}

interface LayoutEdge {
  source: string;
  target: string;
  points: { x: number; y: number }[];
}

interface ContextMenuState {
  x: number;
  y: number;
  nodeId: string;
  label: string;
  isRoot: boolean;
}

interface PrunePromptState {
  nodeId: string;
  label: string;
  instructions: string;
  isSubmitting: boolean;
  error: string | null;
}

const NODE_WIDTH = 220;
const NODE_HEIGHT = 80;
const ROOT_NODE_HEIGHT = 36;

function isRootNode(node: TreeNode): boolean {
  return !getTreeUserContent(node) && !node.assistant_content;
}

function truncate(text: string, max: number): string {
  if (!text) return '';
  const clean = text.replace(/\n/g, ' ').trim();
  return clean.length > max ? clean.slice(0, max) + '...' : clean;
}

function stripFileMention(content: string): string {
  const match = content.match(/^'''USER MENTIONED FILES:\s+.*?\s+'''\n\n[\s\S]*?\n---\n\n/s);
  return match ? content.slice(match[0].length) : content;
}

type TreeViewProps = Readonly<{
  onSelectNode: (nodeId: string) => Promise<void>;
  onDeleteNode: (nodeId: string) => Promise<void>;
}>;

export default function TreeView({ onSelectNode, onDeleteNode }: TreeViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { currentConversation, treeData, loadTree } = useConversationStore();
  const runStates = useRunManager(currentConversation?.id ?? null);

  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [isPanning, setIsPanning] = useState(false);
  const [direction, setDirection] = useState<'TB' | 'LR'>('TB');
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [prunePrompt, setPrunePrompt] = useState<PrunePromptState | null>(null);
  const [summaryModal, setSummaryModal] = useState<{ nodeLabel: string; summary: PruneSummaryRecord } | null>(null);
  const [copiedNodeId, setCopiedNodeId] = useState<string | null>(null);
  const panStartRef = useRef({ x: 0, y: 0, tx: 0, ty: 0 });
  const copiedTimerRef = useRef<number | null>(null);

  const activePruneSummaryNodeIds = useMemo(() => {
    const activeStatuses = new Set(['streaming', 'waiting_approval', 'stopping']);
    const ids = new Set<string>();
    for (const run of runStates) {
      if (!activeStatuses.has(run.status)) continue;
      const metadata = run.metadata || {};
      const slashCommand = metadata.slash_command as { command?: unknown } | undefined;
      const pendingPruneCommand = /^\s*\/(?:prune-summary|prune)\b/i.test(run.pendingUserMessage || '');
      if (slashCommand?.command !== 'prune-summary' && !pendingPruneCommand) continue;
      const targetNodeId = typeof metadata.target_node_id === 'string' && metadata.target_node_id
        ? metadata.target_node_id
        : run.anchorNodeId;
      if (targetNodeId) ids.add(targetNodeId);
    }
    return ids;
  }, [runStates]);

  useEffect(() => {
    if (currentConversation) {
      loadTree(currentConversation.id);
    }
  }, [currentConversation?.id]);

  useEffect(() => () => {
    if (copiedTimerRef.current != null) {
      window.clearTimeout(copiedTimerRef.current);
    }
  }, []);

  // Close context menu on outside click
  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [contextMenu]);

  const { nodes: layoutNodes, edges: layoutEdges, graphWidth, graphHeight } = useMemo(() => {
    if (!treeData || treeData.nodes.length === 0) {
      return { nodes: [], edges: [], graphWidth: 0, graphHeight: 0 };
    }

    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 60, marginx: 40, marginy: 40 });
    g.setDefaultEdgeLabel(() => ({}));

    for (const node of treeData.nodes) {
      const h = isRootNode(node) ? ROOT_NODE_HEIGHT : NODE_HEIGHT;
      g.setNode(node.id, { width: NODE_WIDTH, height: h });
    }

    for (const node of treeData.nodes) {
      if (node.parent_id && g.hasNode(node.parent_id)) {
        g.setEdge(node.parent_id, node.id);
      }
    }

    dagre.layout(g);

    const layoutNodes: LayoutNode[] = [];
    for (const node of treeData.nodes) {
      const dagNode = g.node(node.id);
      if (dagNode) {
        const h = isRootNode(node) ? ROOT_NODE_HEIGHT : NODE_HEIGHT;
        layoutNodes.push({
          id: node.id,
          x: dagNode.x - NODE_WIDTH / 2,
          y: dagNode.y - h / 2,
          width: NODE_WIDTH,
          height: h,
          data: node,
        });
      }
    }

    const layoutEdges: LayoutEdge[] = [];
    g.edges().forEach((e) => {
      const edgeData = g.edge(e);
      if (edgeData && edgeData.points) {
        layoutEdges.push({
          source: e.v,
          target: e.w,
          points: edgeData.points,
        });
      }
    });

    const graphWidth = g.graph().width || 0;
    const graphHeight = g.graph().height || 0;

    return { nodes: layoutNodes, edges: layoutEdges, graphWidth, graphHeight };
  }, [treeData, direction]);

  useEffect(() => {
    if (graphWidth > 0 && graphHeight > 0 && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const padding = 60;
      const scaleX = (rect.width - padding * 2) / graphWidth;
      const scaleY = (rect.height - padding * 2) / graphHeight;
      const scale = Math.min(scaleX, scaleY, 1.2);
      const x = (rect.width - graphWidth * scale) / 2;
      const y = (rect.height - graphHeight * scale) / 2;
      setTransform({ x, y, scale });
    }
  }, [graphWidth, graphHeight]);

  const fitToView = useCallback(() => {
    if (graphWidth > 0 && graphHeight > 0 && containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      const padding = 60;
      const scaleX = (rect.width - padding * 2) / graphWidth;
      const scaleY = (rect.height - padding * 2) / graphHeight;
      const scale = Math.min(scaleX, scaleY, 1.2);
      const x = (rect.width - graphWidth * scale) / 2;
      const y = (rect.height - graphHeight * scale) / 2;
      setTransform({ x, y, scale });
    }
  }, [graphWidth, graphHeight]);

  const handleZoom = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    setTransform((prev) => {
      const newScale = Math.min(Math.max(prev.scale * delta, 0.1), 3);
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return { ...prev, scale: newScale };
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const x = mx - (mx - prev.x) * (newScale / prev.scale);
      const y = my - (my - prev.y) * (newScale / prev.scale);
      return { x, y, scale: newScale };
    });
  }, []);

  const handlePanStart = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0) return;
    setIsPanning(true);
    panStartRef.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y };
  }, [transform.x, transform.y]);

  const handlePanMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning) return;
    const dx = e.clientX - panStartRef.current.x;
    const dy = e.clientY - panStartRef.current.y;
    setTransform((prev) => ({ ...prev, x: panStartRef.current.tx + dx, y: panStartRef.current.ty + dy }));
  }, [isPanning]);

  const handlePanEnd = useCallback(() => {
    setIsPanning(false);
  }, []);

  const handleNodeDoubleClick = useCallback(async (nodeId: string) => {
    if (!currentConversation) return;
    const node = treeData?.nodes.find(n => n.id === nodeId);
    if (!node || isRootNode(node)) return;
    await onSelectNode(nodeId);
  }, [currentConversation, onSelectNode, treeData]);

  const handleContextMenu = useCallback((e: React.MouseEvent, nodeId: string) => {
    e.preventDefault();
    e.stopPropagation();
    const node = treeData?.nodes.find(n => n.id === nodeId);
    if (!node) return;
    const root = isRootNode(node);
    const userContent = node ? getTreeUserContent(node) : '';
    const label = root ? '对话开始' : userContent ? truncate(stripFileMention(userContent), 20) : nodeId.slice(0, 8);
    setContextMenu({ x: e.clientX, y: e.clientY, nodeId, label, isRoot: root });
  }, [treeData]);

  const copyNodeId = useCallback(async (nodeId: string) => {
    try {
      await navigator.clipboard.writeText(nodeId);
      setCopiedNodeId(nodeId);
      if (copiedTimerRef.current != null) {
        window.clearTimeout(copiedTimerRef.current);
      }
      copiedTimerRef.current = window.setTimeout(() => setCopiedNodeId(null), 1200);
    } catch (error) {
      console.error('Failed to copy node id:', error);
    }
  }, []);

  const handleDeleteBranch = useCallback(async () => {
    if (!contextMenu || !currentConversation) return;
    if (!confirm(`确定删除「${contextMenu.label}」及其所有后续分支？`)) return;
    try {
      await onDeleteNode(contextMenu.nodeId);
    } catch (err) {
      console.error('删除失败:', err);
    }
    setContextMenu(null);
  }, [contextMenu, currentConversation, onDeleteNode]);

  const handleGeneratePruneSummary = useCallback(() => {
    if (!contextMenu || !currentConversation) return;
    setPrunePrompt({
      nodeId: contextMenu.nodeId,
      label: contextMenu.label,
      instructions: '',
      isSubmitting: false,
      error: null,
    });
    setContextMenu(null);
  }, [contextMenu, currentConversation]);

  const handleConfirmPruneSummary = useCallback(async () => {
    if (!prunePrompt || !currentConversation) return;
    setPrunePrompt((current) => current ? { ...current, isSubmitting: true, error: null } : current);
    try {
      const instructions = prunePrompt.instructions.trim();
      const content = `/prune-summary node:${prunePrompt.nodeId}${instructions ? ` ${instructions}` : ''}`;
      setPrunePrompt(null);
      void streamManager.startStream(
        currentConversation.id,
        {
          content,
          parent_node_id: prunePrompt.nodeId,
          focus_new_node: false,
          model_id: currentConversation.model_id,
          provider_id: currentConversation.provider_id,
          reasoning_effort: currentConversation.reasoning_effort,
          thinking_enabled: currentConversation.thinking_enabled,
        },
        content,
        prunePrompt.nodeId,
        prunePrompt.nodeId,
      );
    } catch (err: any) {
      setPrunePrompt((current) => current ? {
        ...current,
        isSubmitting: false,
        error: err?.message || '剪枝摘要启动失败',
      } : current);
    }
  }, [prunePrompt, currentConversation]);

  const handleViewPruneSummary = useCallback(() => {
    if (!contextMenu || !treeData) return;
    const node = treeData.nodes.find((item) => item.id === contextMenu.nodeId);
    const summary = node?.context_summaries?.[0];
    if (summary) {
      setSummaryModal({ nodeLabel: contextMenu.label, summary });
    }
    setContextMenu(null);
  }, [contextMenu, treeData]);

  const buildEdgePath = useCallback((edge: LayoutEdge): string => {
    if (!edge.points || edge.points.length < 2) return '';
    const points = edge.points;
    const start = points[0];
    const end = points[points.length - 1];

    if (direction === 'LR') {
      if (points.length === 2) {
        const midX = (start.x + end.x) / 2;
        return `M ${start.x} ${start.y} C ${midX} ${start.y}, ${midX} ${end.y}, ${end.x} ${end.y}`;
      }
      let d = `M ${start.x} ${start.y}`;
      for (let i = 1; i < points.length; i++) {
        const prev = points[i - 1];
        const curr = points[i];
        const midX = (prev.x + curr.x) / 2;
        d += ` C ${midX} ${prev.y}, ${midX} ${curr.y}, ${curr.x} ${curr.y}`;
      }
      return d;
    }

    if (points.length === 2) {
      const midY = (start.y + end.y) / 2;
      return `M ${start.x} ${start.y} C ${start.x} ${midY}, ${end.x} ${midY}, ${end.x} ${end.y}`;
    }

    let d = `M ${start.x} ${start.y}`;
    for (let i = 1; i < points.length; i++) {
      const prev = points[i - 1];
      const curr = points[i];
      const midY = (prev.y + curr.y) / 2;
      d += ` C ${prev.x} ${midY}, ${curr.x} ${midY}, ${curr.x} ${curr.y}`;
    }
    return d;
  }, [direction]);

  if (!currentConversation) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        请先选择一个对话
      </div>
    );
  }

  if (!treeData || layoutNodes.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        暂无对话树数据
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative w-full h-full overflow-hidden"
      style={{ cursor: isPanning ? 'grabbing' : 'grab' }}
      onWheel={handleZoom}
      onMouseDown={handlePanStart}
      onMouseMove={handlePanMove}
      onMouseUp={handlePanEnd}
      onMouseLeave={handlePanEnd}
    >
      <div
        style={{
          transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
          transformOrigin: '0 0',
          position: 'absolute',
          top: 0,
          left: 0,
          width: graphWidth,
          height: graphHeight,
        }}
      >
        <svg width={graphWidth} height={graphHeight} style={{ position: 'absolute', top: 0, left: 0 }}>
          {layoutEdges.map((edge, i) => (
            <path
              key={`edge-${i}`}
              d={buildEdgePath(edge)}
              fill="none"
              stroke="var(--color-muted-foreground)"
              strokeWidth={1.5}
              opacity={0.5}
            />
          ))}
        </svg>

        {layoutNodes.map((node) => {
          const isActive = node.data.is_current;
          const isRoot = isRootNode(node.data);
          const userContent = getTreeUserContent(node.data);
          const pruneSummaryCount = node.data.prune_summary_count || node.data.context_summaries?.length || 0;

          return (
            <div
              key={node.id}
              className="absolute select-none"
              style={{
                left: node.x,
                top: node.y,
                width: node.width,
                height: node.height,
                cursor: isRoot ? 'default' : 'pointer',
              }}
              onDoubleClick={() => handleNodeDoubleClick(node.id)}
              onContextMenu={(e) => handleContextMenu(e, node.id)}
            >
              {isRoot ? (
                <div className="relative w-full h-full flex items-center justify-center">
                  <span className="text-xs text-muted-foreground/70 font-medium">对话开始</span>
                  <TextTooltip content={copiedNodeId === node.id ? '已复制' : '复制节点 ID'}>
                    <button
                      type="button"
                      className="absolute right-1 top-1 inline-flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground/70 hover:bg-accent hover:text-foreground"
                      aria-label="复制节点 ID"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                      }}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void copyNodeId(node.id);
                      }}
                    >
                      {copiedNodeId === node.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                    </button>
                  </TextTooltip>
                </div>
              ) : (
                <div
                  className={`
                    group relative w-full h-full rounded-xl border px-3 py-2 flex flex-col justify-center
                    transition-all duration-150
                    hover:shadow-lg hover:scale-[1.03]
                    ${isActive
                      ? 'border-primary bg-primary/10 shadow-md ring-2 ring-primary/30'
                      : 'border-border bg-card shadow-sm hover:border-primary/40'
                    }
                  `}
                >
                  {userContent && (
                    <p className="text-[11px] leading-tight font-medium text-foreground line-clamp-2 mb-1">
                      {truncate(stripFileMention(userContent), 40)}
                    </p>
                  )}
                  {node.data.assistant_content && (
                    <p className="text-[11px] leading-tight text-muted-foreground line-clamp-2">
                      {truncate(node.data.assistant_content, 50)}
                    </p>
                  )}
                  {node.data.model_id && (
                    <span className="absolute top-1 left-2 max-w-[150px] truncate text-[9px] text-muted-foreground/60">
                      {node.data.model_id}
                    </span>
                  )}
                  <TextTooltip content={copiedNodeId === node.id ? '已复制' : '复制节点 ID'}>
                    <button
                      type="button"
                      className="absolute right-1 top-1 inline-flex h-5 w-5 items-center justify-center rounded-sm text-muted-foreground/70 opacity-0 transition-opacity hover:bg-accent hover:text-foreground group-hover:opacity-100 focus:opacity-100"
                      aria-label="复制节点 ID"
                      onMouseDown={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                      }}
                      onDoubleClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                      }}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void copyNodeId(node.id);
                      }}
                    >
                      {copiedNodeId === node.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                    </button>
                  </TextTooltip>
                  {pruneSummaryCount > 0 && (
                    <TextTooltip content={`${pruneSummaryCount} 份剪枝摘要`}>
                      <span className="absolute bottom-1 right-2 inline-flex h-4 min-w-4 items-center justify-center rounded-sm border border-primary/40 bg-primary/10 px-1 text-[9px] font-medium text-primary">
                        {pruneSummaryCount}
                      </span>
                    </TextTooltip>
                  )}
                  {activePruneSummaryNodeIds.has(node.id) && (
                    <span className="absolute bottom-1 left-2 inline-flex items-center gap-1 text-[9px] text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      摘要中
                    </span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Context Menu */}
      {contextMenu && (
        <div
          className="fixed z-50 min-w-[10rem] rounded-md border bg-popover p-1 shadow-md"
          style={{ left: contextMenu.x, top: contextMenu.y }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent cursor-default"
            onClick={() => {
              void copyNodeId(contextMenu.nodeId);
              setContextMenu(null);
            }}
          >
            {copiedNodeId === contextMenu.nodeId ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            复制节点 ID
          </button>
          <button
            className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent cursor-default"
            onClick={handleGeneratePruneSummary}
          >
            <Scissors className="h-4 w-4" />
            生成剪枝摘要
          </button>
          {(treeData?.nodes.find((node) => node.id === contextMenu.nodeId)?.prune_summary_count || 0) > 0 && (
            <button
              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none hover:bg-accent cursor-default"
              onClick={handleViewPruneSummary}
            >
              <FileText className="h-4 w-4" />
              查看剪枝摘要
            </button>
          )}
          <button
            className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive outline-none hover:bg-destructive/10 disabled:cursor-not-allowed disabled:opacity-50 cursor-default"
            onClick={handleDeleteBranch}
            disabled={contextMenu.isRoot}
          >
            <Trash2 className="h-4 w-4" />
            删除此分支
          </button>
        </div>
      )}

      <Dialog open={!!prunePrompt} onOpenChange={(open) => {
        if (!open) setPrunePrompt(null);
      }}>
        <DialogContent className="max-w-[480px]">
          <DialogHeader>
            <DialogTitle>生成剪枝摘要</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="text-sm text-muted-foreground">
              目标节点：{prunePrompt?.label}
            </div>
            <Textarea
              value={prunePrompt?.instructions || ''}
              onChange={(event) => setPrunePrompt((current) => current ? {
                ...current,
                instructions: event.target.value,
              } : current)}
              placeholder="可选：输入摘要侧重点，例如保留决策依据、工具结果或跨分支差异"
              className="min-h-[120px]"
              disabled={prunePrompt?.isSubmitting}
              autoFocus
            />
            {prunePrompt?.error && (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {prunePrompt.error}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPrunePrompt(null)} disabled={prunePrompt?.isSubmitting}>
              取消
            </Button>
            <Button onClick={handleConfirmPruneSummary} disabled={prunePrompt?.isSubmitting}>
              {prunePrompt?.isSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
              开始生成
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {summaryModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/60 p-4 backdrop-blur-sm">
          <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border bg-popover shadow-xl">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div>
                <h3 className="text-sm font-semibold">剪枝摘要 · {summaryModal.nodeLabel}</h3>
                <p className="text-xs text-muted-foreground">
                  覆盖 {summaryModal.summary.covered_node_ids?.length || 0} 个节点
                </p>
              </div>
              <Button variant="ghost" size="sm" className="h-8 w-8 p-0" onClick={() => setSummaryModal(null)} aria-label="关闭">
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="overflow-auto px-4 py-3">
              <pre className="whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                {summaryModal.summary.summary}
              </pre>
              {(summaryModal.summary.coverage_notes?.length || 0) > 0 && (
                <div className="mt-4 border-t pt-3">
                  <h4 className="mb-2 text-xs font-semibold text-muted-foreground">Coverage</h4>
                  <ul className="space-y-1 text-xs text-muted-foreground">
                    {summaryModal.summary.coverage_notes.map((note, index) => (
                      <li key={index}>- {note}</li>
                    ))}
                  </ul>
                </div>
              )}
              {(summaryModal.summary.truncated_node_ids?.length || 0) > 0 && (
                <p className="mt-3 text-xs text-muted-foreground">
                  可用 /refer node:&lt;node_id&gt; 取回被截断或需要原始证据的轮次。
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Controls */}
      <div className="absolute bottom-4 right-4 flex flex-col gap-1.5 z-10">
        <TextTooltip content={direction === 'TB' ? '切换为水平排列' : '切换为垂直排列'} side="left">
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
            onClick={() => setDirection(d => d === 'TB' ? 'LR' : 'TB')}
            aria-label={direction === 'TB' ? '切换为水平排列' : '切换为垂直排列'}
          >
            {direction === 'TB' ? (
              <ArrowRight className="h-4 w-4" />
            ) : (
              <ArrowDown className="h-4 w-4" />
            )}
          </Button>
        </TextTooltip>
        <TextTooltip content="放大" side="left">
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
            onClick={() =>
              setTransform((prev) => ({ ...prev, scale: Math.min(prev.scale * 1.2, 3) }))
            }
            aria-label="放大"
          >
            <ZoomIn className="h-4 w-4" />
          </Button>
        </TextTooltip>
        <TextTooltip content="缩小" side="left">
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
            onClick={() =>
              setTransform((prev) => ({ ...prev, scale: Math.max(prev.scale * 0.8, 0.1) }))
            }
            aria-label="缩小"
          >
            <ZoomOut className="h-4 w-4" />
          </Button>
        </TextTooltip>
        <TextTooltip content="适应视图" side="left">
          <Button
            variant="outline"
            size="sm"
            className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
            onClick={fitToView}
            aria-label="适应视图"
          >
            <Maximize2 className="h-4 w-4" />
          </Button>
        </TextTooltip>
      </div>
    </div>
  );
}

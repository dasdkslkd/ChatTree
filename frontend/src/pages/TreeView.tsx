import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import dagre from 'dagre';
import { useConversationStore, conversationStore } from '../store/conversationStore';
import { useNavigationStore } from '../store/navigationStore';
import { Button } from '@/components/ui/button';
import { ZoomIn, ZoomOut, Maximize2, ArrowDown, ArrowRight, Trash2 } from 'lucide-react';
import type { TreeNode } from '../api/conversation';
import { conversationApi } from '../api/conversation';

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
}

const NODE_WIDTH = 220;
const NODE_HEIGHT = 80;
const ROOT_NODE_HEIGHT = 36;

function isRootNode(node: TreeNode): boolean {
  return !node.user_content && !node.assistant_content;
}

function truncate(text: string, max: number): string {
  if (!text) return '';
  const clean = text.replace(/\n/g, ' ').trim();
  return clean.length > max ? clean.slice(0, max) + '...' : clean;
}

export default function TreeView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const { currentConversation, treeData, loadTree } = useConversationStore();
  const { setChatViewMode } = useNavigationStore();

  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const [isPanning, setIsPanning] = useState(false);
  const [direction, setDirection] = useState<'TB' | 'LR'>('TB');
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const panStartRef = useRef({ x: 0, y: 0, tx: 0, ty: 0 });

  useEffect(() => {
    if (currentConversation) {
      loadTree(currentConversation.id);
    }
  }, [currentConversation?.id]);

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
    const { switchNode } = conversationStore.getState();
    await switchNode(nodeId);
    setChatViewMode('chat');
  }, [currentConversation, treeData, setChatViewMode]);

  const handleContextMenu = useCallback((e: React.MouseEvent, nodeId: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (isRootNode(treeData?.nodes.find(n => n.id === nodeId) as TreeNode)) return;
    const node = treeData?.nodes.find(n => n.id === nodeId);
    const label = node?.user_content ? truncate(node.user_content, 20) : nodeId.slice(0, 8);
    setContextMenu({ x: e.clientX, y: e.clientY, nodeId, label });
  }, [treeData]);

  const handleDeleteBranch = useCallback(async () => {
    if (!contextMenu || !currentConversation) return;
    if (!confirm(`确定删除「${contextMenu.label}」及其所有后续分支？`)) return;
    try {
      await conversationApi.deleteNode(currentConversation.id, contextMenu.nodeId);
      await loadTree(currentConversation.id);
      const { selectConversation } = useConversationStore.getState();
      await selectConversation(currentConversation.id);
    } catch (err) {
      console.error('删除失败:', err);
    }
    setContextMenu(null);
  }, [contextMenu, currentConversation, loadTree]);

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
                <div className="w-full h-full flex items-center justify-center">
                  <span className="text-xs text-muted-foreground/70 font-medium">对话开始</span>
                </div>
              ) : (
                <div
                  className={`
                    w-full h-full rounded-xl border px-3 py-2 flex flex-col justify-center
                    transition-all duration-150
                    hover:shadow-lg hover:scale-[1.03]
                    ${isActive
                      ? 'border-primary bg-primary/10 shadow-md ring-2 ring-primary/30'
                      : 'border-border bg-card shadow-sm hover:border-primary/40'
                    }
                  `}
                >
                  {node.data.user_content && (
                    <p className="text-[11px] leading-tight font-medium text-foreground line-clamp-2 mb-1">
                      {truncate(node.data.user_content, 40)}
                    </p>
                  )}
                  {node.data.assistant_content && (
                    <p className="text-[11px] leading-tight text-muted-foreground line-clamp-2">
                      {truncate(node.data.assistant_content, 50)}
                    </p>
                  )}
                  {node.data.model_id && (
                    <span className="absolute top-1 right-2 text-[9px] text-muted-foreground/60 font-mono">
                      {node.data.model_id}
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
            className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive outline-none hover:bg-destructive/10 cursor-default"
            onClick={handleDeleteBranch}
          >
            <Trash2 className="h-4 w-4" />
            删除此分支
          </button>
        </div>
      )}

      {/* Controls */}
      <div className="absolute bottom-4 right-4 flex flex-col gap-1.5 z-10">
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
          onClick={() => setDirection(d => d === 'TB' ? 'LR' : 'TB')}
          title={direction === 'TB' ? '切换为水平排列' : '切换为垂直排列'}
        >
          {direction === 'TB' ? (
            <ArrowRight className="h-4 w-4" />
          ) : (
            <ArrowDown className="h-4 w-4" />
          )}
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
          onClick={() =>
            setTransform((prev) => ({ ...prev, scale: Math.min(prev.scale * 1.2, 3) }))
          }
          title="放大"
        >
          <ZoomIn className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
          onClick={() =>
            setTransform((prev) => ({ ...prev, scale: Math.max(prev.scale * 0.8, 0.1) }))
          }
          title="缩小"
        >
          <ZoomOut className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-8 w-8 p-0 bg-background/80 backdrop-blur-sm"
          onClick={fitToView}
          title="适应视图"
        >
          <Maximize2 className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

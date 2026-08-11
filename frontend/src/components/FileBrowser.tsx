import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  ChevronRight,
  Copy,
  File,
  Folder,
  FolderOpen,
  FolderTree,
  Pencil,
  Trash2,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import { filesApi, type FileEntry } from '../api/files';
import { getApiErrorMessage } from '../api/errors';
import { MarkdownView } from './markdown/MarkdownView';
import { SyntaxHighlighter, oneDark } from './markdown/languages';
import { Button } from './ui/button';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog';
import { Input } from './ui/input';
import { TextTooltip } from './ui/text-tooltip';
import { FileContextMenu, type ContextMenuItem } from './FileContextMenu';

interface TreeNode {
  name: string;
  path: string;
  type: 'dir' | 'file';
  size: number;
  expanded: boolean;
  loading: boolean;
  children: TreeNode[];
}

interface MenuTarget {
  x: number;
  y: number;
  kind: 'file' | 'dir' | 'tab';
  path: string;
  name: string;
}

function extToLanguage(name: string): string | null {
  const dot = name.lastIndexOf('.');
  if (dot < 0) return null;
  const ext = name.slice(dot + 1).toLowerCase();
  const map: Record<string, string> = {
    js: 'javascript', jsx: 'jsx', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'tsx', mts: 'typescript',
    py: 'python', rb: 'ruby', rs: 'rust', go: 'go',
    java: 'java', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', hpp: 'cpp',
    cs: 'csharp', php: 'php', css: 'css', scss: 'scss', less: 'less',
    html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup',
    json: 'json', jsonc: 'json',
    yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini', cfg: 'ini',
    sql: 'sql', sh: 'bash', bash: 'bash', zsh: 'bash',
    ps1: 'powershell', pwsh: 'powershell',
    diff: 'diff', patch: 'diff',
  };
  return map[ext] ?? null;
}

function joinPath(parent: string, name: string): string {
  return `${parent.replace(/[\\/]+$/, '')}/${name}`;
}

function pathParent(path: string): string | null {
  const trimmed = path.replace(/[\\/]+$/, '');
  if (!trimmed) return null;
  const idx = trimmed.lastIndexOf('/');
  if (idx < 0) return null;
  return trimmed.slice(0, idx) || null;
}

function toNode(entry: FileEntry, parentPath: string): TreeNode {
  return {
    name: entry.name,
    path: joinPath(parentPath, entry.name),
    type: entry.type,
    size: entry.size,
    expanded: false,
    loading: false,
    children: [],
  };
}

/** 按路径在树中查找节点（含子目录，递归）。 */
function findNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const node of nodes) {
    if (node.path === path) return node;
    if (node.type === 'dir' && node.children.length) {
      const found = findNode(node.children, path);
      if (found) return found;
    }
  }
  return null;
}

/** 按路径重建树，仅替换目标节点为新对象，其余引用保持不变。 */
function updateNodeInTree(nodes: TreeNode[], path: string, updater: (node: TreeNode) => TreeNode): TreeNode[] {
  return nodes.map((node) => {
    if (node.path === path) return updater(node);
    if (node.type === 'dir' && node.children.length) {
      const newer = updateNodeInTree(node.children, path, updater);
      if (newer !== node.children) return { ...node, children: newer };
    }
    return node;
  });
}

export function FileBrowser({ root }: { root: string }) {
  const [currentPath, setCurrentPath] = useState(root);
  const [nodes, setNodes] = useState<TreeNode[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [openFiles, setOpenFiles] = useState<{ path: string; name: string }[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [treeOpen, setTreeOpen] = useState(true);
  const [treeWidth, setTreeWidth] = useState(260);
  const [menu, setMenu] = useState<MenuTarget | null>(null);
  const [renameNode, setRenameNode] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<{ path: string; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const tabBarRef = useRef<HTMLDivElement | null>(null);
  const treeDragRef = useRef<{ startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setNodes(null);
    setSelectedPath(null);
    setOpenFiles([]);
    setActivePath(null);
    filesApi
      .list(currentPath)
      .then((data) => setNodes(data.entries.map((entry) => toNode(entry, currentPath))))
      .catch((loadError) => setError(getApiErrorMessage(loadError, '加载目录失败')))
      .finally(() => setLoading(false));
  }, [currentPath]);

  const openNode = useCallback((node: TreeNode) => {
    if (node.type === 'dir') return;
    setOpenFiles((files) =>
      files.some((f) => f.path === node.path)
        ? files
        : [...files, { path: node.path, name: node.name }]
    );
    setActivePath(node.path);
  }, []);

  const selectNode = useCallback((node: TreeNode) => {
    setSelectedPath(node.path);
    openNode(node);
  }, [openNode]);

  const closeFile = useCallback((path: string) => {
    const next = openFiles.filter((f) => f.path !== path);
    setOpenFiles(next);
    setSelectedPath((sel) => (sel === path ? null : sel));
    if (activePath === path) {
      if (next.length === 0) {
        setActivePath(null);
      } else {
        const idx = openFiles.findIndex((f) => f.path === path);
        setActivePath(next[Math.min(idx, next.length - 1)].path);
      }
    }
  }, [activePath, openFiles]);

  const toggleDir = useCallback((dirPath: string) => {
    const dir = nodes ? findNode(nodes, dirPath) : null;
    if (!dir) return;
    if (dir.expanded) {
      setNodes((curr) => (curr ? updateNodeInTree(curr, dirPath, (node) => ({ ...node, expanded: false })) : curr));
      return;
    }
    setNodes((curr) => (curr ? updateNodeInTree(curr, dirPath, (node) => ({ ...node, loading: true })) : curr));
    filesApi
      .list(dirPath)
      .then((data) => {
        const children = data.entries.map((entry) => toNode(entry, dirPath));
        setNodes((curr) => (curr ? updateNodeInTree(curr, dirPath, (node) => ({ ...node, loading: false, expanded: true, children })) : curr));
      })
      .catch(() => {
        setNodes((curr) => (curr ? updateNodeInTree(curr, dirPath, (node) => ({ ...node, loading: false, expanded: false })) : curr));
      });
  }, [nodes]);

  useEffect(() => {
    const el = tabBarRef.current;
    if (!el) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      el.scrollLeft += event.deltaY || event.deltaX;
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const beginTreeDrag = useCallback((event: React.PointerEvent) => {
    event.preventDefault();
    treeDragRef.current = { startX: event.clientX, startWidth: treeWidth };
    const handle = event.currentTarget as HTMLElement;
    handle.setPointerCapture(event.pointerId);
  }, [treeWidth]);

  const moveTreeDrag = useCallback((event: React.PointerEvent) => {
    const drag = treeDragRef.current;
    if (!drag) return;
    const width = drag.startWidth - (event.clientX - drag.startX);
    setTreeWidth(Math.min(480, Math.max(180, Math.round(width))));
  }, []);

  const endTreeDrag = useCallback(() => {
    treeDragRef.current = null;
  }, []);

  // ── 右键菜单 ─────────────────────────────────────────────────────────
  const reloadTop = useCallback((dirPath: string) => {
    filesApi
      .list(dirPath)
      .then((data) => setNodes(data.entries.map((entry) => toNode(entry, dirPath))))
      .catch((loadError) => setError(getApiErrorMessage(loadError, '加载目录失败')));
  }, []);

  const openRowMenu = useCallback((node: TreeNode, event: React.MouseEvent) => {
    event.preventDefault();
    setMenu({ x: event.clientX, y: event.clientY, kind: node.type, path: node.path, name: node.name });
  }, []);

  const openTabMenu = useCallback((file: { path: string; name: string }, event: React.MouseEvent) => {
    event.preventDefault();
    setMenu({ x: event.clientX, y: event.clientY, kind: 'tab', path: file.path, name: file.name });
  }, []);

  const renameRef = useRef({ path: '', original: '', value: '' });

  const openRename = useCallback((target: MenuTarget) => {
    renameRef.current = { path: target.path, original: target.name, value: target.name };
    setRenameNode(target.path);
    setRenameValue(target.name);
  }, []);

  const openDelete = useCallback((target: MenuTarget) => {
    setDeleteTarget({ path: target.path, name: target.name });
  }, []);

  const copyText = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      toast.error('复制失败');
    }
  }, []);

  const relativePath = (path: string) => {
    const base = root.replace(/\\/g, '/');
    const p = path.replace(/\\/g, '/');
    return p.startsWith(`${base}/`) ? p.slice(base.length + 1) : p;
  };

  const confirmRename = useCallback(async () => {
    const { path, original, value } = renameRef.current;
    const newName = value.trim();
    setRenameNode(null);
    if (!newName || newName === original) return;
    setBusy(true);
    try {
      const res = await filesApi.rename(path, newName);
      setOpenFiles((files) => files.map((f) => (f.path === path ? { ...f, path: res.path, name: newName } : f)));
      setActivePath((p) => (p === path ? res.path : p));
      setSelectedPath((p) => (p === path ? res.path : p));
      reloadTop(currentPath);
    } catch (loadError) {
      toast.error(getApiErrorMessage(loadError, '重命名失败'));
    } finally {
      setBusy(false);
    }
  }, [currentPath, reloadTop]);

  const cancelRename = useCallback(() => setRenameNode(null), []);

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      const target = deleteTarget.path;
      const isRemoved = (p: string) => p === target || p.startsWith(`${target}/`);
      await filesApi.delete(target);
      setOpenFiles((files) => files.filter((f) => !isRemoved(f.path)));
      setActivePath((p) => (p && isRemoved(p) ? null : p));
      setSelectedPath((p) => (p && isRemoved(p) ? null : p));
      reloadTop(currentPath);
    } catch (loadError) {
      toast.error(getApiErrorMessage(loadError, '删除失败'));
    } finally {
      setBusy(false);
      setDeleteTarget(null);
    }
  };

  const menuItems: ContextMenuItem[] = menu
    ? [
        {
          key: 'reveal',
          label: '在文件资源管理器中显示',
          icon: <FolderOpen className="h-3.5 w-3.5" />,
          onSelect: () =>
            filesApi.reveal(menu.path).catch((loadError) => toast.error(getApiErrorMessage(loadError, '定位失败'))),
        },
        {
          key: 'copy',
          label: '复制路径',
          icon: <Copy className="h-3.5 w-3.5" />,
          onSelect: () => copyText(menu.path.replace(/\\/g, '/')),
        },
        {
          key: 'copyRelative',
          label: '复制相对路径',
          icon: <Copy className="h-3.5 w-3.5" />,
          disabled: !menu.path.startsWith(`${root}/`),
          onSelect: () => copyText(relativePath(menu.path)),
        },
        ...(menu.kind !== 'tab'
          ? [{
              key: 'rename',
              label: '重命名',
              icon: <Pencil className="h-3.5 w-3.5" />,
              separatorBefore: true,
              onSelect: () => openRename(menu),
            }]
          : []),
        {
          key: 'delete',
          label: '删除',
          icon: <Trash2 className="h-3.5 w-3.5" />,
          destructive: true,
          onSelect: () => openDelete(menu),
        },
      ]
    : [];

  const treeBody = useMemo(() => {
    if (loading) return <Empty text="加载中..." />;
    if (error) return <Empty text={error} />;
    if (!nodes || nodes.length === 0) return <Empty text="空目录" />;
    return nodes.map((node) => (
      <TreeRow
        key={node.path}
        node={node}
        depth={0}
        selectedPath={selectedPath}
        onSelect={selectNode}
        onToggleDir={toggleDir}
        onContextMenu={openRowMenu}
        editingPath={renameNode}
        renameValue={renameValue}
        onRenameChange={setRenameValue}
        onRenameCommit={confirmRename}
        onRenameCancel={cancelRename}
      />
    ));
  }, [loading, error, nodes, selectedPath, selectNode, toggleDir, renameNode, renameValue, confirmRename, cancelRename]);

  return (
    <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col">
      {/* 标签栏：左侧滚动标签 + 右侧固定文件树折叠按钮 */}
      <div className="flex shrink-0 items-center border-b" style={{ borderColor: 'var(--border)' }}>
        <div
          ref={tabBarRef}
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto px-1.5 py-1"
          style={{ scrollbarWidth: 'thin', scrollbarColor: 'var(--fg-tertiary) transparent' }}
        >
          <div className="flex min-w-0 flex-1 items-center gap-0.5">
            {openFiles.map((file) => {
              const active = file.path === activePath;
              return (
                <TextTooltip key={file.path} content={file.path}>
                  <button
                    type="button"
                    onClick={() => setActivePath(file.path)}
                    onContextMenu={(event) => openTabMenu(file, event)}
                    className="group flex max-w-44 shrink-0 items-center gap-1 rounded px-2 py-1 text-xs"
                    style={{
                      background: active ? 'var(--accent)' : 'transparent',
                      color: active ? 'var(--primary)' : 'var(--fg-85)',
                      boxShadow: active ? 'inset 0 -2px 0 var(--primary)' : undefined,
                    }}
                  >
                    <span className="truncate">{file.name}</span>
                    <span
                      role="button"
                      tabIndex={-1}
                      onClick={(event) => { event.stopPropagation(); closeFile(file.path); }}
                      aria-label={`关闭 ${file.name}`}
                      className="shrink-0 rounded opacity-0 hover:bg-[var(--bg-button-tertiary-hover)] group-hover:opacity-100"
                    >
                      <X className="h-3 w-3" style={{ color: 'var(--fg-tertiary)' }} />
                    </span>
                  </button>
                </TextTooltip>
              );
            })}
            {openFiles.length === 0 && (
              <span className="truncate px-2 text-xs" style={{ color: 'var(--fg-tertiary)' }}>文件浏览</span>
            )}
          </div>
        </div>
        <Button
          variant={treeOpen ? 'secondary' : 'ghost'}
          size="icon-sm"
          className="mx-1 shrink-0"
          onClick={() => setTreeOpen((open) => !open)}
          aria-label="切换文件树"
        >
          <FolderTree className="h-4 w-4" />
        </Button>
      </div>

      {/* 主体：左侧文件查看 + 右侧文件树，中间分割线可拖动 */}
      <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
        {/* 左：文件查看器 */}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {activePath ? (
            <FileViewer
              key={activePath}
              path={activePath}
              name={openFiles.find((f) => f.path === activePath)?.name ?? ''}
            />
          ) : (
            <Empty text="从文件树中选择文件以查看内容" />
          )}
        </div>

        {/* 分割线 */}
        {treeOpen && (
          <div
            role="separator"
            aria-orientation="vertical"
            tabIndex={0}
            className="shrink-0 cursor-col-resize border-l"
            style={{ borderColor: 'var(--border)', width: 3 }}
            onPointerDown={beginTreeDrag}
            onPointerMove={moveTreeDrag}
            onPointerUp={endTreeDrag}
            onPointerCancel={endTreeDrag}
          />
        )}

        {/* 右：文件树 */}
        {treeOpen && (
          <div className="flex shrink-0 flex-col" style={{ width: treeWidth }}>
            <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2" style={{ borderColor: 'var(--border)' }}>
              <Button
                variant="ghost"
                size="icon-sm"
                className="shrink-0"
                disabled={!pathParent(currentPath)}
                onClick={() => { const p = pathParent(currentPath); if (p) setCurrentPath(p); }}
                aria-label="上级目录"
              >
                <ArrowLeft className="h-4 w-4" />
              </Button>
              <TextTooltip content={currentPath}>
                <div className="min-w-0 flex-1 truncate text-xs" style={{ color: 'var(--fg-secondary)' }}>
                  {currentPath}
                </div>
              </TextTooltip>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden py-1 custom-scrollbar">
              {treeBody}
            </div>
          </div>
        )}
      </div>

      {menu && (
        <FileContextMenu x={menu.x} y={menu.y} items={menuItems} onClose={() => setMenu(null)} />
      )}

      <Dialog
        open={!!deleteTarget}
        onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除确认</DialogTitle>
          </DialogHeader>
          <div className="text-sm" style={{ color: 'var(--fg-85)' }}>
            确定删除「{deleteTarget?.name}」吗？此操作不可撤销。
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button onClick={confirmDelete} disabled={busy}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

const TreeRow = memo(function TreeRow({
  node,
  depth,
  selectedPath,
  onSelect,
  onToggleDir,
  onContextMenu,
  editingPath,
  renameValue,
  onRenameChange,
  onRenameCommit,
  onRenameCancel,
}: {
  node: TreeNode;
  depth: number;
  selectedPath: string | null;
  onSelect: (node: TreeNode) => void;
  onToggleDir: (path: string) => void;
  onContextMenu: (node: TreeNode, event: React.MouseEvent) => void;
  editingPath: string | null;
  renameValue: string;
  onRenameChange: (value: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
}) {
  const isDir = node.type === 'dir';
  const editing = node.path === editingPath;
  return (
    <Fragment>
      <div
        role="button"
        tabIndex={0}
        onClick={() => (isDir ? onToggleDir(node.path) : onSelect(node))}
        onKeyDown={(event) => {
          if (event.key === 'Enter') (isDir ? onToggleDir(node.path) : onSelect(node));
        }}
        onContextMenu={(event) => onContextMenu(node, event)}
        className="flex cursor-pointer select-none items-center gap-1 py-1 pr-2 text-sm hover:bg-[var(--bg-button-tertiary-hover)]"
        style={{
          paddingLeft: 8 + depth * 14,
          background: node.path === selectedPath ? 'var(--bg-button-tertiary-hover)' : undefined,
          color: 'var(--fg-85)',
        }}
      >
        <ChevronRight
          className={`h-3.5 w-3.5 shrink-0 transition-transform ${isDir && node.expanded ? 'rotate-90' : ''}`}
          style={{ color: 'var(--fg-tertiary)', visibility: isDir ? 'visible' : 'hidden' }}
        />
        {isDir ? (
          node.expanded
            ? <FolderOpen className="h-4 w-4 shrink-0" style={{ color: 'var(--fg-secondary)' }} />
            : <Folder className="h-4 w-4 shrink-0" style={{ color: 'var(--fg-secondary)' }} />
        ) : (
          <File className="h-4 w-4 shrink-0" style={{ color: 'var(--fg-secondary)' }} />
        )}
        {editing ? (
          <Input
            value={renameValue}
            onChange={(event) => onRenameChange(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
            onClick={(event) => event.stopPropagation()}
            onContextMenu={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (event.key === 'Enter') onRenameCommit();
              else if (event.key === 'Escape') onRenameCancel();
            }}
            onBlur={onRenameCommit}
            autoFocus
            className="h-6 min-w-0 flex-1 px-1 py-0 text-sm"
          />
        ) : (
          <span className="min-w-0 truncate">{node.name}</span>
        )}
      </div>
      {isDir && node.expanded && (
        <div>
          {node.loading
            ? <Empty text="加载中..." />
            : node.children.map((child) => (
              <TreeRow
                key={child.path}
                node={child}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelect={onSelect}
                onToggleDir={onToggleDir}
                onContextMenu={onContextMenu}
                editingPath={editingPath}
                renameValue={renameValue}
                onRenameChange={onRenameChange}
                onRenameCommit={onRenameCommit}
                onRenameCancel={onRenameCancel}
              />
            ))}
        </div>
      )}
    </Fragment>
  );
});

const FileViewer = memo(function FileViewer({ path, name }: { path: string; name: string }) {
  const [state, setState] = useState<{ loading: boolean; error: string | null; data: { content: string; binary: boolean; truncated: boolean } | null }>({
    loading: true,
    error: null,
    data: null,
  });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, error: null, data: null });
    filesApi
      .content(path)
      .then((data) => {
        if (!cancelled) setState({ loading: false, error: null, data });
      })
      .catch((loadError) => {
        if (!cancelled) setState({ loading: false, error: getApiErrorMessage(loadError, '加载文件失败'), data: null });
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  if (state.loading) return <Empty text="加载中..." />;
  if (state.error) return <Empty text={state.error} />;
  if (!state.data) return null;

  const { content, binary, truncated } = state.data;
  const isMarkdown = name.toLowerCase().endsWith('.md');
  const language = isMarkdown ? null : extToLanguage(name);
  const lineCount = content.split('\n').length;
  const tooLarge = lineCount > 4000;

  return (
    <div className="min-h-0 w-full max-w-full flex-1 overflow-auto custom-scrollbar">
      {binary ? (
        <Empty text="二进制文件，无法预览。使用「在系统应用中打开」查看完整内容。" />
      ) : truncated ? (
        <Empty text="文件过大，仅预览前 256KB。使用「在系统应用中打开」查看完整内容。" />
      ) : tooLarge && isMarkdown ? (
        <Empty text={`文件行数较多（${lineCount} 行），为保持流畅已关闭预览。使用「在系统应用中打开」查看完整内容。`} />
      ) : tooLarge ? (
        <div>
          <div className="border-b px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
            文件行数较多（{lineCount} 行），已显示为纯文本。使用「在系统应用中打开」查看高亮版本。
          </div>
          <pre className="whitespace-pre-wrap break-words px-3 py-2 text-xs" style={{ color: 'var(--fg-85)' }}>{content}</pre>
        </div>
      ) : isMarkdown ? (
        <div className="px-3 py-2 text-sm">
          <MarkdownView content={content} />
        </div>
      ) : language ? (
        <SyntaxHighlighter
          language={language}
          style={oneDark}
          customStyle={{ margin: 0, padding: '10px 12px', background: 'transparent', fontSize: 13, lineHeight: '20px', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
          codeTagProps={{ style: { fontFamily: 'var(--font-mono, "JetBrains Mono", ui-monospace, monospace)', display: 'block', width: '100%', whiteSpace: 'pre-wrap', wordBreak: 'break-word' } }}
        >
          {content.replace(/\n$/, '')}
        </SyntaxHighlighter>
      ) : (
        <pre className="whitespace-pre-wrap break-words px-3 py-2 text-sm" style={{ color: 'var(--fg-85)' }}>{content}</pre>
      )}
    </div>
  );
});

function Empty({ text }: { text: string }) {
  return <div className="px-3 py-4 text-xs" style={{ color: 'var(--fg-tertiary)' }}>{text}</div>;
}
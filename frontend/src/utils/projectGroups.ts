import type { Conversation, WorkspaceContext } from '../types/conversation';

export const DEFAULT_VISIBLE_HISTORY_COUNT = 5;

export interface ProjectGroup {
  id: string;
  label: string;
  path: string;
  workspace: WorkspaceContext;
  conversations: Conversation[];
  isCollapsed?: boolean;
  isHistoryExpanded?: boolean;
}

export interface GroupOptions {
  defaultWorkspace?: WorkspaceContext | null;
  extraWorkspaces?: WorkspaceContext[];
  collapsedProjectIds?: Set<string>;
  expandedHistoryProjectIds?: Set<string>;
  searchQuery?: string;
  projectOrder?: string[];
  projectVisibility?: Record<string, boolean>;
}

export interface VisibleProjectConversations {
  items: Conversation[];
  hiddenCount: number;
  canExpand: boolean;
  canCollapse: boolean;
}

export function groupConversationsByProject(
  conversations: Conversation[],
  options: GroupOptions = {},
): ProjectGroup[] {
  const defaultWorkspace = normalizeWorkspace(options.defaultWorkspace);
  const query = (options.searchQuery || '').trim().toLowerCase();
  const byPath = new Map<string, ProjectGroup>();

  for (const conversation of conversations) {
    if (query && !conversationMatches(conversation, query)) continue;
    const workspace = normalizeWorkspace(conversation.workspace, defaultWorkspace);
    const path = workspace.cwd;
    if (!isProjectVisible(path, options.projectVisibility)) continue;
    const existing = byPath.get(path);
    if (existing) {
      existing.conversations.push(conversation);
      continue;
    }
    const id = encodeProjectId(path);
    byPath.set(path, {
      id,
      label: workspace.label || labelFromPath(path),
      path,
      workspace,
      conversations: [conversation],
      isCollapsed: options.collapsedProjectIds?.has(id) || false,
      isHistoryExpanded: options.expandedHistoryProjectIds?.has(id) || false,
    });
  }

  for (const extraWorkspace of options.extraWorkspaces || []) {
    const workspace = normalizeWorkspace(extraWorkspace, defaultWorkspace);
    if (!workspace.cwd || byPath.has(workspace.cwd)) continue;
    if (!isProjectVisible(workspace.cwd, options.projectVisibility)) continue;
    if (query && !workspaceMatches(workspace, query)) continue;
    const id = encodeProjectId(workspace.cwd);
    byPath.set(workspace.cwd, {
      id,
      label: workspace.label || labelFromPath(workspace.cwd),
      path: workspace.cwd,
      workspace,
      conversations: [],
      isCollapsed: options.collapsedProjectIds?.has(id) || false,
      isHistoryExpanded: options.expandedHistoryProjectIds?.has(id) || false,
    });
  }

  const groups = [...byPath.values()];
  for (const group of groups) {
    group.conversations.sort(sortByUpdatedAtDesc);
  }
  const orderIndex = new Map((options.projectOrder || []).map((id, index) => [id, index]));
  groups.sort((a, b) => {
    const aOrder = orderIndex.get(a.id);
    const bOrder = orderIndex.get(b.id);
    if (aOrder !== undefined || bOrder !== undefined) {
      return (aOrder ?? Number.MAX_SAFE_INTEGER) - (bOrder ?? Number.MAX_SAFE_INTEGER);
    }
    const latestDiff = latestUpdatedAt(b) - latestUpdatedAt(a);
    if (latestDiff !== 0) return latestDiff;
    return a.label.localeCompare(b.label);
  });
  return groups;
}

export function getVisibleProjectConversations(
  group: ProjectGroup,
  limit = DEFAULT_VISIBLE_HISTORY_COUNT,
): VisibleProjectConversations {
  if (group.isCollapsed) {
    return { items: [], hiddenCount: group.conversations.length, canExpand: false, canCollapse: false };
  }
  if (group.isHistoryExpanded || group.conversations.length <= limit) {
    return {
      items: group.conversations,
      hiddenCount: 0,
      canExpand: false,
      canCollapse: group.conversations.length > limit,
    };
  }
  return {
    items: group.conversations.slice(0, limit),
    hiddenCount: group.conversations.length - limit,
    canExpand: true,
    canCollapse: false,
  };
}

export function getWorkspaceForNewConversation(
  groups: ProjectGroup[],
  selectedProjectId: string | null | undefined,
  defaultWorkspace?: WorkspaceContext | null,
): WorkspaceContext {
  const selected = groups.find((group) => group.id === selectedProjectId);
  if (selected) return selected.workspace;
  if (groups[0]) return groups[0].workspace;
  return normalizeWorkspace(defaultWorkspace);
}

export function encodeProjectId(path: string): string {
  return encodeURIComponent(path).replace(/[!'()*]/g, (char) =>
    `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

export function normalizeWorkspace(
  workspace?: WorkspaceContext | null,
  fallback?: WorkspaceContext | null,
): WorkspaceContext {
  const source = workspace || fallback;
  const cwd = source?.cwd || '';
  const roots = source?.workspace_roots?.length ? source.workspace_roots : (cwd ? [cwd] : []);
  return {
    cwd,
    workspace_roots: roots,
    protected_paths: source?.protected_paths || [],
    label: source?.label || labelFromPath(cwd),
  };
}

function conversationMatches(conversation: Conversation, query: string): boolean {
  const haystack = [
    conversation.title,
    conversation.workspace?.cwd,
    conversation.workspace?.label,
  ].filter(Boolean).join(' ').toLowerCase();
  return haystack.includes(query);
}

function workspaceMatches(workspace: WorkspaceContext, query: string): boolean {
  const haystack = [
    workspace.cwd,
    workspace.label,
  ].filter(Boolean).join(' ').toLowerCase();
  return haystack.includes(query);
}

function sortByUpdatedAtDesc(a: Conversation, b: Conversation): number {
  return (b.updated_at || 0) - (a.updated_at || 0);
}

function latestUpdatedAt(group: ProjectGroup): number {
  return group.conversations[0]?.updated_at || 0;
}

function labelFromPath(path: string): string {
  const cleaned = path.replace(/[\\/]+$/, '');
  const parts = cleaned.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || '默认项目';
}

export function isProjectVisible(path: string, visibility?: Record<string, boolean>): boolean {
  if (!visibility) return true;
  if (visibility[path] === false) return false;
  const canonical = canonicalProjectPath(path);
  return !Object.entries(visibility).some(([candidate, visible]) =>
    visible === false && canonicalProjectPath(candidate) === canonical
  );
}

function canonicalProjectPath(path: string): string {
  return path.replace(/[\\/]+/g, '/').replace(/\/+$/, '').toLowerCase();
}

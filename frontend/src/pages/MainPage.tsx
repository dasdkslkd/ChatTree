import {
  useEffect,
  useState,
  useRef,
  useLayoutEffect,
  useCallback,
  useMemo,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { TextTooltip } from '@/components/ui/text-tooltip';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import {
  Plus, X, MoreHorizontal, ChevronRight, Square,
  Check, Pencil, Loader2, Network, MessageSquare, FileText, Download, FolderOpen, FolderPlus, Search, Settings,
  PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, ArrowLeft,
} from 'lucide-react';
import { conversationApi } from '../api/conversation';
import { configApi } from '../api/config';
import { messageApi } from '../api/message';
import type { TaskStateSnapshot } from '../api/taskState';
import { transcriptService } from '../services/transcript';
import {
  createTranscriptRequestCoordinator,
  type TranscriptRequestCoordinator,
} from '../services/transcriptRequestCoordinator';
import { taskStateCoordinator } from '../services/taskStateCoordinator';
import {
  ConversationSyncCoordinator,
  type ConversationSyncRequest,
  type ConversationSyncResult,
} from '../services/conversationSyncCoordinator';
import type {
  SendMessageRequest,
  ToolPermissionMode,
  TaskContextMode,
} from '../types/message';
import type { ActiveTaskRecord } from '../types/task';
import type { PlanApprovalItem, PlanQuestionItem, ToolApprovalItem, TranscriptItem, UserMessageItem } from '../types/transcript';
import type { MultiAgentMode, WorkspaceContext } from '../types/conversation';
import type { ProjectCapabilityConfig } from '../types/model';
import { useConversationStore } from '../store/conversationStore';
import { useModelStore } from '../store/modelStore';
import { useNavigationStore } from '../store/navigationStore';
import { getProfileContext } from '../runtime/profileContext';
import {
  ImportAssetMutationOwner,
  ImportAssetMutationQueue,
  ImportAssetPreviewCache,
} from '../runtime/importAssetPreview';
import {
  LEFT_SIDEBAR_STORAGE_KEY,
  RIGHT_PANEL_STORAGE_KEY,
  profileStorageKey,
} from '../runtime/profileStorage';
import { useRunManager } from '../hooks/useRunManager';
import { streamManager, type StreamState } from '../services/streamManager';
import { slashRegistry } from '../services/slashRegistry';
import {
  getStoppableRunIdsForSelectedBranch,
  isDetachedRunView,
  isRunBlockingSelectedBranch,
  isRunVisibleInSelectedTranscript,
  shouldPatchRunIntoMainConversation,
} from '../utils/runVisibility';
import { resolveSendNodeId, resolveSlashStreamNodeId } from '../utils/sendTarget';
import { getSlashRunLabel, shouldQueueForMainThread, shouldRenderRunDraft } from '../utils/slashRuntime';
import {
  groupDetachedSideRuns,
  getWorkflowProgressSteps,
  buildSidePanelDraft,
  type SideRunGroupItem,
  type SideRunDraft,
} from '../utils/sideRunGrouping';
import {
  SIDE_RUN_KINDS,
  createQueuedMessageId,
} from '../utils/identifiers';
import {
  createToolPermissionDraft,
  getConfiguredDefaultToolPermissionMode,
  syncToolPermissionDraftFromBranch,
  normalizeToolPermissionMode,
  type ToolPermissionDraft,
} from '../utils/toolPermissionDraft';
import {
  getActiveStreamPollingDelay,
  getStreamStatusText as getStreamStatusLabel,
} from '../utils/streaming';
import { ChatInput } from '../components/ChatInput';
import { TranscriptList } from '../components/transcript/TranscriptList';
import TreeView from './TreeView';
import {
  getVisibleProjectConversations,
  getWorkspaceForNewConversation,
  groupConversationsByProject,
  encodeProjectId,
  isProjectVisible,
} from '../utils/projectGroups';
import {
  LEFT_SIDEBAR_WIDTH,
  RIGHT_PANEL_WIDTH,
  getKeyboardResizedSidebarWidth,
  getPointerResizedSidebarWidth,
  readStoredSidebarWidth,
  writeStoredSidebarWidth,
  type SidebarResizeSide,
  type SidebarWidthConfig,
} from '../utils/sidebarResize';
import {
  applyTranscriptPatch,
  normalizeTranscriptItems,
  stateFromTranscriptSnapshot,
  findTranscriptAnchorElement,
  isTranscriptItemVisibleNow,
  isTranscriptItemOnCurrentBranch,
  getTranscriptItemNodeId,
  getTranscriptItemMessageId,
  getEditableUserMessageAttachmentRefs,
  userMessageItemReferencesAttachment,
  type TranscriptState,
  type TranscriptScrollTarget,
} from '../utils/transcriptItems';
import { createTaskPanelItem } from '../utils/activeTask';
import { MarkdownView, ThinkingBlock } from '../components/markdown/MarkdownView';
import { SyntaxHighlighter, oneDark } from '../components/markdown/languages';
import {
  getBrowserStorage,
  loadManualProjectWorkspaces,
  saveManualProjectWorkspaces,
  loadProjectOrder,
  saveProjectOrder,
  mergeManualProjectWorkspace,
} from '../utils/projectStorage';
import {
  normalizeTaskContextMode,
  getBranchToolPermissionMode,
  getBranchTaskContextMode,
} from '../utils/branchMode';
import { formatConversationTime } from '../utils/time';

const PROFILE_ID = getProfileContext().profileId;
const PROFILE_LEFT_SIDEBAR_STORAGE_KEY = profileStorageKey(PROFILE_ID, LEFT_SIDEBAR_STORAGE_KEY);
const PROFILE_RIGHT_PANEL_STORAGE_KEY = profileStorageKey(PROFILE_ID, RIGHT_PANEL_STORAGE_KEY);
type SidebarResizeSession = {
  side: SidebarResizeSide;
  startClientX: number;
  startWidth: number;
  storageKey: string;
  config: SidebarWidthConfig;
};

function getCurrentVisibleTranscriptTip(): { conversationId: string; tipNodeId: string } | null {
  const state = useConversationStore.getState();
  const conversationId = state.currentConversation?.id;
  const tipNodeId = state.currentNodeId || state.currentConversation?.current_node_id || null;
  return conversationId && tipNodeId ? { conversationId, tipNodeId } : null;
}

type QueuedMessage = {
  id: string;
  conversationId: string;
  nodeId?: string | null;
  anchorNodeId?: string | null;
  blockingRunIds?: string[];
  content: string;
  request: SendMessageRequest;
};

/* ---------- Component ---------- */
export default function ChatPage() {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [outlineCollapsed, setOutlineCollapsed] = useState(false);
  const [rightPanelView, setRightPanelView] = useState<'outline' | 'side' | 'tasks'>('outline');
  const [selectedSideRunId, setSelectedSideRunId] = useState<string | null>(null);
  const [leftSidebarWidth, setLeftSidebarWidth] = useState(() =>
    readStoredSidebarWidth(getBrowserStorage(), PROFILE_LEFT_SIDEBAR_STORAGE_KEY, LEFT_SIDEBAR_WIDTH),
  );
  const [rightPanelWidth, setRightPanelWidth] = useState(() =>
    readStoredSidebarWidth(getBrowserStorage(), PROFILE_RIGHT_PANEL_STORAGE_KEY, RIGHT_PANEL_WIDTH),
  );
  const [resizingSidebar, setResizingSidebar] = useState<SidebarResizeSide | null>(null);
  const [scrollPositions, setScrollPositions] = useState<Record<string, number>>({});
  const [isScrolling, setIsScrolling] = useState(false);
  const [editValue, setEditValue] = useState<string | null>(null);
  const [editTargetNodeId, setEditTargetNodeId] = useState<string | null>(null);
  const [editToolPermissionMode, setEditToolPermissionMode] = useState<ToolPermissionMode | null>(null);
  const [editReturnNodeId, setEditReturnNodeId] = useState<string | null>(null);
  const [editProtectedAttachmentNames, setEditProtectedAttachmentNames] = useState<string[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<string[]>([]);
  const [attachedImageRefs, setAttachedImageRefs] = useState<Array<{ filename: string; mime_type?: string }>>([]);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [hiddenSideRunIdsByConversation, setHiddenSideRunIdsByConversation] = useState<Record<string, string[]>>({});
  const [activeTask, setActiveTask] = useState<ActiveTaskRecord | null>(null);
  const [taskContextMode, setTaskContextMode] = useState<TaskContextMode>('attached');
  const [transcriptItems, setTranscriptItems] = useState<TranscriptItem[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [planActionPending, setPlanActionPending] = useState<string | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [toolApprovalPending, setToolApprovalPending] = useState<string | null>(null);
  const [toolApprovalError, setToolApprovalError] = useState<string | null>(null);
  const [, setCopiedTranscriptRunId] = useState<string | null>(null);
  const [toolPermissionDraft, setToolPermissionDraftState] = useState<ToolPermissionDraft>(() => createToolPermissionDraft());
  const [newConversationMultiAgentMode, setNewConversationMultiAgentMode] = useState<MultiAgentMode>('explicit_request_only');
  const [previewImage, setPreviewImage] = useState<{ name: string; url: string } | null>(null);
  const [conversationSearch, setConversationSearch] = useState('');
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [projectPickerSearch, setProjectPickerSearch] = useState('');
  const [collapsedProjectIds, setCollapsedProjectIds] = useState<Set<string>>(() => new Set());
  const [expandedHistoryProjectIds, setExpandedHistoryProjectIds] = useState<Set<string>>(() => new Set());
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const modelConfig = useModelStore((state) => state.config);
  const defaultToolPermissionMode = getConfiguredDefaultToolPermissionMode(modelConfig);
  const [manualProjectWorkspaces, setManualProjectWorkspaces] = useState<WorkspaceContext[]>(() => loadManualProjectWorkspaces());
  const [projectOrder, setProjectOrder] = useState<string[]>(() => loadProjectOrder());
  const [projectConfigs, setProjectConfigs] = useState<Record<string, ProjectCapabilityConfig>>({});
  const [draggingProjectId, setDraggingProjectId] = useState<string | null>(null);
  const [projectFolderDialogMode, setProjectFolderDialogMode] = useState<'create' | 'existing' | null>(null);
  const [projectFolderPath, setProjectFolderPath] = useState('');
  const [projectFolderLabel, setProjectFolderLabel] = useState('');
  const [projectFolderError, setProjectFolderError] = useState('');
  const [projectFolderSubmitting, setProjectFolderSubmitting] = useState(false);
  const [, refreshImportPreviews] = useState(0);
  const importAssetPreviewCacheRef = useRef<ImportAssetPreviewCache | null>(null);
  const importAssetMutationOwnerRef = useRef<ImportAssetMutationOwner | null>(null);
  const importAssetMutationQueueRef = useRef<ImportAssetMutationQueue | null>(null);
  if (!importAssetPreviewCacheRef.current) {
    importAssetPreviewCacheRef.current = new ImportAssetPreviewCache(
      conversationApi.fetchImportBlob,
    );
  }
  if (!importAssetMutationOwnerRef.current) {
    importAssetMutationOwnerRef.current = new ImportAssetMutationOwner();
  }
  if (!importAssetMutationQueueRef.current) {
    importAssetMutationQueueRef.current = new ImportAssetMutationQueue();
  }
  const importAssetPreviewCache = importAssetPreviewCacheRef.current;
  const importAssetMutationOwner = importAssetMutationOwnerRef.current;
  const importAssetMutationQueue = importAssetMutationQueueRef.current;
  const scrollTimeoutRef = useRef<number | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const conversationSearchInputRef = useRef<HTMLInputElement>(null);
  const pendingScrollId = useRef<string | null>(null);
  const sidebarResizeRef = useRef<SidebarResizeSession | null>(null);
  const autoScrollRef = useRef(true);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  const toolPermissionDraftRef = useRef<ToolPermissionDraft>(toolPermissionDraft);
  const transcriptRequestCoordinatorRef = useRef<TranscriptRequestCoordinator | null>(null);
  const transcriptStateRef = useRef<TranscriptState>({
    conversationId: null,
    nodeId: null,
    revision: 0,
    items: [],
  });
  if (!transcriptRequestCoordinatorRef.current) {
    transcriptRequestCoordinatorRef.current = createTranscriptRequestCoordinator({
      fetchSnapshot: transcriptService.fetchBranchSnapshot,
      getVisibleTarget: getCurrentVisibleTranscriptTip,
      onLoadingChange: setTranscriptLoading,
      onSnapshot: (snapshot) => {
        const next = stateFromTranscriptSnapshot(snapshot);
        transcriptStateRef.current = next;
        setTranscriptItems(normalizeTranscriptItems(next.items));
      },
      onErrorChange: (error) => setTranscriptError(
        error ? '对话 transcript 刷新失败，已保留当前内容' : null,
      ),
    });
  }

  useEffect(
    () => importAssetPreviewCache.subscribe(
      () => refreshImportPreviews((revision) => revision + 1),
    ),
    [importAssetPreviewCache],
  );

  const beginSidebarResize = useCallback((
    event: ReactPointerEvent<HTMLDivElement>,
    side: SidebarResizeSide,
  ) => {
    if (event.button !== 0) return;

    const session = side === 'left'
      ? {
          side,
          startClientX: event.clientX,
          startWidth: leftSidebarWidth,
          storageKey: PROFILE_LEFT_SIDEBAR_STORAGE_KEY,
          config: LEFT_SIDEBAR_WIDTH,
        }
      : {
          side,
          startClientX: event.clientX,
          startWidth: rightPanelWidth,
          storageKey: PROFILE_RIGHT_PANEL_STORAGE_KEY,
          config: RIGHT_PANEL_WIDTH,
        };

    sidebarResizeRef.current = session;
    setResizingSidebar(side);
    event.currentTarget.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  }, [leftSidebarWidth, rightPanelWidth]);

  const adjustSidebarWidthFromKeyboard = useCallback((
    event: ReactKeyboardEvent<HTMLDivElement>,
    side: SidebarResizeSide,
  ) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;

    event.preventDefault();

    if (side === 'left') {
      const nextWidth = getKeyboardResizedSidebarWidth(side, event.key, leftSidebarWidth, LEFT_SIDEBAR_WIDTH);
      setLeftSidebarWidth(writeStoredSidebarWidth(
        getBrowserStorage(),
        PROFILE_LEFT_SIDEBAR_STORAGE_KEY,
        nextWidth,
        LEFT_SIDEBAR_WIDTH,
      ));
      return;
    }

    const nextWidth = getKeyboardResizedSidebarWidth(side, event.key, rightPanelWidth, RIGHT_PANEL_WIDTH);
    setRightPanelWidth(writeStoredSidebarWidth(
      getBrowserStorage(),
      PROFILE_RIGHT_PANEL_STORAGE_KEY,
      nextWidth,
      RIGHT_PANEL_WIDTH,
    ));
  }, [leftSidebarWidth, rightPanelWidth]);

  useEffect(() => {
    if (!resizingSidebar) return;

    const applyWidth = (session: SidebarResizeSession, width: number) => {
      const storedWidth = writeStoredSidebarWidth(getBrowserStorage(), session.storageKey, width, session.config);
      if (session.side === 'left') {
        setLeftSidebarWidth(storedWidth);
      } else {
        setRightPanelWidth(storedWidth);
      }
    };

    const handlePointerMove = (event: PointerEvent) => {
      const session = sidebarResizeRef.current;
      if (!session) return;

      applyWidth(session, getPointerResizedSidebarWidth(
        session.side,
        session.startWidth,
        session.startClientX,
        event.clientX,
        session.config,
      ));
    };

    const finishResize = () => {
      sidebarResizeRef.current = null;
      setResizingSidebar(null);
    };

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finishResize);
    window.addEventListener('pointercancel', finishResize);

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finishResize);
      window.removeEventListener('pointercancel', finishResize);
    };
  }, [resizingSidebar]);

  const { chatViewMode, toggleChatViewMode, openSettings } = useNavigationStore();

  const updateToolPermissionDraft = useCallback((draft: ToolPermissionDraft) => {
    toolPermissionDraftRef.current = draft;
    setToolPermissionDraftState(draft);
  }, []);

  const getToolPermissionDraft = useCallback(() => toolPermissionDraftRef.current, []);

  const updateQueuedMessages = useCallback((updater: (messages: QueuedMessage[]) => QueuedMessage[]) => {
    const next = updater(queuedMessagesRef.current);
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  const isAtBottom = useCallback(() => {
    if (!historyRef.current) return true;
    const { scrollTop, scrollHeight, clientHeight } = historyRef.current;
    return scrollHeight - scrollTop - clientHeight <= 8;
  }, []);

  const scrollToBottom = useCallback(() => {
    const container = historyRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, []);

  const handleScroll = useCallback(() => {
    setIsScrolling(true);
    autoScrollRef.current = isAtBottom();
    if (scrollTimeoutRef.current) clearTimeout(scrollTimeoutRef.current);
    scrollTimeoutRef.current = window.setTimeout(() => setIsScrolling(false), 1000);
  }, [isAtBottom]);

  const {
    conversations, currentConversation,
    currentNodeId, pendingScrollNodeId, clearPendingScroll,
    createConversation, selectConversation, deleteConversation, deleteNode, switchNode, loadConversations, loadTree,
    clearCurrentConversation, updateConversationTitle, refreshMessages, refreshBranches,
  } = useConversationStore();

  useEffect(() => {
    setPreviewImage(null);
    return () => {
      importAssetMutationOwner.clear();
      importAssetPreviewCache.clear();
    };
  }, [currentConversation?.id, importAssetMutationOwner, importAssetPreviewCache]);

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) return;
    for (const { filename } of attachedImageRefs) {
      void importAssetPreviewCache.load(conversationId, filename).catch(() => {});
    }
  }, [attachedImageRefs, currentConversation?.id, importAssetPreviewCache]);

  const [renameDialogOpen, setRenameDialogOpen] = useState(false);
  const [renameConversationId, setRenameConversationId] = useState<string | null>(null);
  const [renameTitle, setRenameTitle] = useState('');

  const handleRenameClick = (id: string, currentTitle: string) => {
    setRenameConversationId(id);
    setRenameTitle(currentTitle || '');
    setRenameDialogOpen(true);
  };

  const handleRenameConfirm = async () => {
    if (renameConversationId && renameTitle.trim()) {
      await updateConversationTitle(renameConversationId, renameTitle.trim());
    }
    setRenameDialogOpen(false);
    setRenameConversationId(null);
    setRenameTitle('');
  };

  const loadProjectConfigs = useCallback(async () => {
    try {
      const data = await configApi.getProjects();
      setProjectConfigs(data.config || {});
    } catch {
      setProjectConfigs({});
    }
  }, []);

  useEffect(() => {
    void loadProjectConfigs();
  }, [loadProjectConfigs]);

  useEffect(() => {
    const reloadProjects = () => {
      void loadProjectConfigs();
      void loadConversations();
    };
    window.addEventListener('chattree-projects-updated', reloadProjects);
    return () => window.removeEventListener('chattree-projects-updated', reloadProjects);
  }, [loadConversations, loadProjectConfigs]);

  const handleRenameCancel = () => {
    setRenameDialogOpen(false);
    setRenameConversationId(null);
    setRenameTitle('');
  };

  const activeRunStates = useRunManager(currentConversation?.id ?? null);
  const currentConversationIdRef = useRef<string | null>(null);
  currentConversationIdRef.current = currentConversation?.id ?? null;
  const conversationSyncCoordinatorRef = useRef<ConversationSyncCoordinator | null>(null);
  const scheduleConversationSyncRef = useRef<(
    conversationId: string,
    request: ConversationSyncRequest,
  ) => Promise<ConversationSyncResult>>(async () => ({ messagesConfirmed: false }));
  const loadTranscriptSnapshot = useCallback(async (
    conversationId: string | null | undefined,
    tipNodeId?: string | null,
  ) => {
    if (!conversationId || !tipNodeId) {
      transcriptRequestCoordinatorRef.current?.cancelActive();
      transcriptStateRef.current = { conversationId: null, nodeId: null, revision: 0, items: [] };
      setTranscriptItems([]);
      setTranscriptError(null);
      setTranscriptLoading(false);
      return;
    }
    return transcriptRequestCoordinatorRef.current?.request({
      conversationId,
      tipNodeId,
    });
  }, []);
  const refreshVisibleTranscriptSnapshot = useCallback(async (conversationId?: string | null) => {
    const visible = getCurrentVisibleTranscriptTip();
    if (!visible) return;
    if (conversationId && visible.conversationId !== conversationId) return;
    await loadTranscriptSnapshot(visible.conversationId, visible.tipNodeId);
  }, [loadTranscriptSnapshot]);
  const hiddenSideRunIds = useMemo(() => {
    const conversationId = currentConversation?.id;
    return new Set(conversationId ? hiddenSideRunIdsByConversation[conversationId] ?? [] : []);
  }, [currentConversation?.id, hiddenSideRunIdsByConversation]);
  const sidePanelRunStates = useMemo(
    () => activeRunStates.filter((run) => !hiddenSideRunIds.has(run.runId)),
    [activeRunStates, hiddenSideRunIds],
  );
  const hideSideRun = useCallback((conversationId: string, runId: string) => {
    setHiddenSideRunIdsByConversation((current) => {
      const existing = current[conversationId] ?? [];
      if (existing.includes(runId)) return current;
      return {
        ...current,
        [conversationId]: [...existing, runId],
      };
    });
  }, []);
  const applyTaskStateSnapshot = useCallback((
    conversationId: string,
    state: TaskStateSnapshot,
  ) => {
    if (conversationId !== currentConversationIdRef.current) return;
    setActiveTask(state.task);
  }, []);

  const refreshTaskState = useCallback(async (conversationId: string | null | undefined) => {
    if (!conversationId) {
      setActiveTask(null);
      return null;
    }
    try {
      const state = await taskStateCoordinator.refresh(conversationId);
      applyTaskStateSnapshot(conversationId, state);
      return state;
    } catch (error) {
      console.error('刷新 TaskState 失败:', error);
      return null;
    }
  }, [applyTaskStateSnapshot]);

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId) {
      setActiveTask(null);
      return;
    }
    setActiveTask(null);
    const unsubscribe = taskStateCoordinator.subscribe(conversationId, (state) => {
      applyTaskStateSnapshot(conversationId, state);
    });
    void taskStateCoordinator.refresh(conversationId).catch((error) => {
      console.error('刷新 TaskState 失败:', error);
    });
    return unsubscribe;
  }, [applyTaskStateSnapshot, currentConversation?.id]);
  useEffect(() => {
    void slashRegistry.refresh().catch(() => {});
  }, []);
  const startStreaming = useCallback(
    async (
      convId: string,
      request: SendMessageRequest,
      pending: string | null = null,
      requestNodeId?: string,
      anchorNodeId?: string | null,
    ) => {
      await streamManager.startStream(convId, request, pending, requestNodeId, anchorNodeId);
    },
    [],
  );
  const [localStreamingConversationCounts, setLocalStreamingConversationCounts] = useState<Map<string, number>>(() => new Map());
  const [backendActiveStreamConversationCounts, setBackendActiveStreamConversationCounts] = useState<Map<string, number>>(() => new Map());
  const activeStreamConversationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const [id, count] of localStreamingConversationCounts) counts.set(id, Math.max(counts.get(id) ?? 0, count));
    for (const [id, count] of backendActiveStreamConversationCounts) counts.set(id, Math.max(counts.get(id) ?? 0, count));
    return counts;
  }, [localStreamingConversationCounts, backendActiveStreamConversationCounts]);
  const projectDragMovedRef = useRef(false);
  const projectGroupRefs = useRef(new Map<string, HTMLDivElement>());
  const projectFlipFirstRef = useRef<Map<string, number> | null>(null);

  const currentBranchNodeIds = useMemo(
    () => new Set(transcriptItems.flatMap((item) => {
      const nodeId = getTranscriptItemNodeId(item);
      return nodeId ? [nodeId] : [];
    })),
    [transcriptItems],
  );
  const selectedBranchTipId = currentNodeId || currentConversation?.current_node_id || null;
  const currentBranchToolPermissionMode = useMemo(
    () => getBranchToolPermissionMode(transcriptItems, selectedBranchTipId),
    [transcriptItems, selectedBranchTipId],
  );
  const liveBranchToolPermissionMode = useMemo(() => {
    const runs = activeRunStates
      .filter((run) => isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds))
      .filter((run) => normalizeToolPermissionMode(run.toolPermissionMode))
      .sort((a, b) => b.createdAt - a.createdAt);
    return normalizeToolPermissionMode(runs[0]?.toolPermissionMode);
  }, [activeRunStates, currentBranchNodeIds, selectedBranchTipId]);
  const currentBranchTaskContextMode = useMemo(
    () => getBranchTaskContextMode(transcriptItems, selectedBranchTipId),
    [transcriptItems, selectedBranchTipId],
  );
  const liveBranchTaskContextMode = useMemo(() => {
    const runs = activeRunStates
      .filter((run) => isRunVisibleInSelectedTranscript(run, selectedBranchTipId, currentBranchNodeIds))
      .filter((run) => normalizeTaskContextMode(run.taskContextMode))
      .sort((a, b) => b.createdAt - a.createdAt);
    return normalizeTaskContextMode(runs[0]?.taskContextMode);
  }, [activeRunStates, currentBranchNodeIds, selectedBranchTipId]);
  useEffect(() => {
    const next = syncToolPermissionDraftFromBranch(
      toolPermissionDraftRef.current,
      liveBranchToolPermissionMode ?? currentBranchToolPermissionMode,
    );
    if (next !== toolPermissionDraftRef.current) {
      updateToolPermissionDraft(next);
    }
  }, [currentBranchToolPermissionMode, liveBranchToolPermissionMode, updateToolPermissionDraft]);

  useEffect(() => {
    setTaskContextMode(liveBranchTaskContextMode ?? currentBranchTaskContextMode ?? 'attached');
  }, [currentBranchTaskContextMode, liveBranchTaskContextMode, currentConversation?.id]);

  useEffect(() => {
    if (currentConversation || toolPermissionDraftRef.current.explicit) return;
    const current = toolPermissionDraftRef.current;
    if (current.mode !== defaultToolPermissionMode) {
      updateToolPermissionDraft(createToolPermissionDraft(defaultToolPermissionMode));
    }
  }, [currentConversation, defaultToolPermissionMode, updateToolPermissionDraft]);

  const sideRunDrafts = useMemo(() => sidePanelRunStates
    .filter((run) => shouldRenderRunDraft(run))
    .map((run) => {
      if (!isDetachedRunView(run, selectedBranchTipId, currentBranchNodeIds)) return null;
      return buildSidePanelDraft(run);
    })
    .filter((draft): draft is SideRunDraft => Boolean(draft))
    .filter((draft) => draft.showPendingBubble || draft.showStreamBlock),
    [currentBranchNodeIds, selectedBranchTipId, sidePanelRunStates],
  );

  const sideRunGroups = useMemo(
    () => groupDetachedSideRuns(sideRunDrafts),
    [sideRunDrafts],
  );
  const sideRunTopLevelCount = useMemo(
    () => sideRunGroups.reduce((total, group) => total + group.runs.length, 0),
    [sideRunGroups],
  );
  const selectedSideRunItem = useMemo((): SideRunGroupItem<SideRunDraft> | null => {
    if (!selectedSideRunId) return null;
    for (const group of sideRunGroups) {
      for (const item of group.runs) {
        if (item.run.runId === selectedSideRunId) return item;
        const step = item.steps.find((candidate) => candidate.run.runId === selectedSideRunId);
        if (step) {
          return {
            draft: step,
            run: step.run,
            steps: [],
          };
        }
      }
    }
    const selectedRun = sidePanelRunStates.find((run) => run.runId === selectedSideRunId)
      || activeRunStates.find((run) => run.runId === selectedSideRunId);
    if (selectedRun && SIDE_RUN_KINDS.has(selectedRun.kind) && shouldRenderRunDraft(selectedRun)) {
      const draft = buildSidePanelDraft(selectedRun);
      const steps = activeRunStates
        .filter((run) => run.createdByRunId === selectedRun.runId)
        .filter((run) => SIDE_RUN_KINDS.has(run.kind))
        .filter((run) => shouldRenderRunDraft(run))
        .map(buildSidePanelDraft)
        .sort((a, b) => (a.run.createdAt || 0) - (b.run.createdAt || 0));
      return {
        draft,
        run: selectedRun,
        steps,
      };
    }
    return null;
  }, [activeRunStates, selectedSideRunId, sidePanelRunStates, sideRunGroups]);

  const currentBranchStreamingRunIds = useMemo(
    () => activeRunStates
      .filter((run) => isRunBlockingSelectedBranch(run, selectedBranchTipId, currentBranchNodeIds))
      .map((run) => run.runId),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const currentBranchStoppableRunIds = useMemo(
    () => getStoppableRunIdsForSelectedBranch(activeRunStates, selectedBranchTipId, currentBranchNodeIds),
    [activeRunStates, currentBranchNodeIds, selectedBranchTipId],
  );
  const currentBranchHasStreamingChat = currentBranchStreamingRunIds.length > 0;
  const taskPanelItem = useMemo(() => createTaskPanelItem(activeTask), [activeTask]);
  const taskPanelOpenCount = taskPanelItem ? 1 : 0;
  const displayTranscriptItems = transcriptItems;

  useEffect(() => {
    const conversationId = currentConversation?.id;
    if (!conversationId || !selectedBranchTipId) {
      transcriptStateRef.current = { conversationId: null, nodeId: null, revision: 0, items: [] };
      setTranscriptItems([]);
      setTranscriptError(null);
      setTranscriptLoading(false);
      return;
    }
    const transcriptState = transcriptStateRef.current;
    if (
      transcriptState.conversationId === conversationId
      && transcriptState.nodeId === selectedBranchTipId
    ) {
      return;
    }
    void loadTranscriptSnapshot(conversationId, selectedBranchTipId);
  }, [currentConversation?.id, loadTranscriptSnapshot, selectedBranchTipId]);

  useEffect(
    () => () => transcriptRequestCoordinatorRef.current?.cancelActive(),
    [],
  );

  useEffect(() => {
    if (selectedSideRunId && !selectedSideRunItem) {
      setSelectedSideRunId(null);
    }
  }, [selectedSideRunId, selectedSideRunItem]);

  const visibleQueuedMessages = useMemo(
    () => queuedMessages
      .filter((message) =>
        message.conversationId === currentConversation?.id
        && (!message.nodeId || message.nodeId === selectedBranchTipId)
      )
      .map(({ id, content }) => ({ id, content })),
    [queuedMessages, currentConversation?.id, selectedBranchTipId],
  );
  const projectVisibility = useMemo(
    () => Object.fromEntries(
      Object.entries(projectConfigs).map(([path, project]) => [path, project.visible !== false]),
    ),
    [projectConfigs],
  );
  const defaultWorkspace = useMemo(
    () => conversations.find((conversation) =>
      conversation.workspace?.cwd && isProjectVisible(conversation.workspace.cwd, projectVisibility)
    )?.workspace || null,
    [conversations, projectVisibility],
  );
  const projectGroups = useMemo(
    () => groupConversationsByProject(conversations, {
      defaultWorkspace,
      extraWorkspaces: manualProjectWorkspaces,
      collapsedProjectIds,
      expandedHistoryProjectIds,
      searchQuery: conversationSearch,
      projectOrder,
      projectVisibility,
    }),
    [conversations, defaultWorkspace, manualProjectWorkspaces, collapsedProjectIds, expandedHistoryProjectIds, conversationSearch, projectOrder, projectVisibility],
  );
  const allProjectGroups = useMemo(
    () => groupConversationsByProject(conversations, {
      defaultWorkspace,
      extraWorkspaces: manualProjectWorkspaces,
      collapsedProjectIds,
      expandedHistoryProjectIds,
      projectOrder,
      projectVisibility,
    }),
    [conversations, defaultWorkspace, manualProjectWorkspaces, collapsedProjectIds, expandedHistoryProjectIds, projectOrder, projectVisibility],
  );
  const measureProjectGroupTops = useCallback((skipProjectId?: string) => {
    const tops = new Map<string, number>();
    for (const [projectId, element] of projectGroupRefs.current.entries()) {
      if (projectId === skipProjectId) continue;
      element.getAnimations().forEach((animation) => animation.cancel());
      tops.set(projectId, element.getBoundingClientRect().top);
    }
    return tops;
  }, []);

  useLayoutEffect(() => {
    const first = projectFlipFirstRef.current;
    if (!first) return;
    projectFlipFirstRef.current = null;

    window.requestAnimationFrame(() => {
      for (const group of projectGroups) {
        if (group.id === draggingProjectId) continue;
        const element = projectGroupRefs.current.get(group.id);
        const previousTop = first.get(group.id);
        if (!element || previousTop == null) continue;
        const delta = previousTop - element.getBoundingClientRect().top;
        if (!delta) continue;
        element.animate(
          [
            { transform: `translateY(${delta}px)` },
            { transform: 'translateY(0)' },
          ],
          { duration: 240, easing: 'cubic-bezier(.22,1,.36,1)' },
        );
      }
    });
  }, [draggingProjectId, projectGroups]);

  const selectedNewConversationWorkspace = useMemo(
    () => getWorkspaceForNewConversation(allProjectGroups, selectedProjectId, defaultWorkspace),
    [allProjectGroups, selectedProjectId, defaultWorkspace],
  );

  useEffect(() => {
    if (!allProjectGroups.length) return;
    if (!selectedProjectId || !allProjectGroups.some((group) => group.id === selectedProjectId)) {
      setSelectedProjectId(allProjectGroups[0].id);
    }
  }, [allProjectGroups, selectedProjectId]);

  useEffect(() => {
    if (!currentConversation?.workspace?.cwd) return;
    const group = allProjectGroups.find((item) => item.path === currentConversation.workspace?.cwd);
    if (group && group.id !== selectedProjectId) {
      setSelectedProjectId(group.id);
    }
  }, [currentConversation?.workspace?.cwd, allProjectGroups, selectedProjectId]);

  const toggleProjectCollapsed = (projectId: string) => {
    setCollapsedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const updateProjectOrder = useCallback((order: string[]) => {
    setProjectOrder(order);
    saveProjectOrder(order);
  }, []);

  const handleProjectDragStart = useCallback((event: DragEvent, projectId: string) => {
    projectDragMovedRef.current = false;
    setDraggingProjectId(projectId);
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', projectId);
  }, []);

  const handleProjectDragOver = useCallback((event: DragEvent, targetProjectId: string) => {
    if (!draggingProjectId || draggingProjectId === targetProjectId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';

    const ids = allProjectGroups.map((group) => group.id);
    const fromIndex = ids.indexOf(draggingProjectId);
    const targetIndex = ids.indexOf(targetProjectId);
    if (fromIndex < 0 || targetIndex < 0) return;

    const rect = event.currentTarget.getBoundingClientRect();
    const insertAfterTarget = event.clientY > rect.top + rect.height / 2;
    const withoutDragged = ids.filter((id) => id !== draggingProjectId);
    const targetIndexAfterRemoval = withoutDragged.indexOf(targetProjectId);
    const insertIndex = targetIndexAfterRemoval + (insertAfterTarget ? 1 : 0);
    withoutDragged.splice(insertIndex, 0, draggingProjectId);

    if (withoutDragged.join('\u0000') !== ids.join('\u0000')) {
      projectDragMovedRef.current = true;
      projectFlipFirstRef.current = measureProjectGroupTops(draggingProjectId);
      updateProjectOrder(withoutDragged);
    }
  }, [allProjectGroups, draggingProjectId, measureProjectGroupTops, updateProjectOrder]);

  const handleProjectDragEnd = useCallback(() => {
    setDraggingProjectId(null);
    window.setTimeout(() => {
      projectDragMovedRef.current = false;
    }, 100);
  }, []);

  const toggleHistoryExpanded = (projectId: string) => {
    setExpandedHistoryProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  const workspaceForCreateRequest = () => (
    selectedNewConversationWorkspace.cwd ? selectedNewConversationWorkspace : undefined
  );

  const rememberProjectWorkspace = (workspace: WorkspaceContext) => {
    const next = mergeManualProjectWorkspace(manualProjectWorkspaces, workspace);
    setManualProjectWorkspaces(next);
    saveManualProjectWorkspaces(next);
    setSelectedProjectId(encodeProjectId(workspace.cwd));
  };

  const openProjectFolderDialog = (mode: 'create' | 'existing') => {
    setProjectFolderDialogMode(mode);
    setProjectFolderPath('');
    setProjectFolderLabel('');
    setProjectFolderError('');
  };

  const closeProjectFolderDialog = () => {
    if (projectFolderSubmitting) return;
    setProjectFolderDialogMode(null);
    setProjectFolderPath('');
    setProjectFolderLabel('');
    setProjectFolderError('');
  };

  const handleProjectFolderSubmit = async () => {
    const path = projectFolderPath.trim();
    if (!projectFolderDialogMode || !path) {
      setProjectFolderError('请输入文件夹路径');
      return;
    }
    setProjectFolderSubmitting(true);
    setProjectFolderError('');
    try {
      const label = projectFolderLabel.trim() || undefined;
      const workspace = projectFolderDialogMode === 'create'
        ? await conversationApi.createProjectFolder(path, label)
        : await conversationApi.resolveProjectFolder(path, label);
      rememberProjectWorkspace(workspace);
      setProjectPickerSearch('');
      setProjectFolderDialogMode(null);
      setProjectFolderPath('');
      setProjectFolderLabel('');
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || '项目文件夹处理失败';
      setProjectFolderError(String(detail));
    } finally {
      setProjectFolderSubmitting(false);
    }
  };

  const selectedProjectGroup = allProjectGroups.find((group) => group.id === selectedProjectId) || allProjectGroups[0] || null;
  const newChatProjectLabel = selectedNewConversationWorkspace.label || '默认项目';
  const filteredProjectGroups = projectPickerSearch.trim()
    ? allProjectGroups.filter((group) => {
        const query = projectPickerSearch.trim().toLowerCase();
        return `${group.label} ${group.path}`.toLowerCase().includes(query);
      })
    : allProjectGroups;
  const handleProjectPickerOpenChange = (open: boolean) => {
    setProjectPickerOpen(open);
    if (!open) setProjectPickerSearch('');
  };

  const projectSettingsSlot = (
    <DropdownMenu open={projectPickerOpen} onOpenChange={handleProjectPickerOpenChange}>
      <TextTooltip content={selectedProjectGroup?.path || selectedNewConversationWorkspace.cwd || '默认项目'}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="new-chat-setting-chip"
            aria-label="选择项目"
          >
            <FolderOpen className="h-4 w-4" />
            <span className="truncate">{selectedProjectGroup?.label || selectedNewConversationWorkspace.label || '默认项目'}</span>
            <ChevronRight className="h-3.5 w-3.5 rotate-90 opacity-70" />
          </button>
        </DropdownMenuTrigger>
      </TextTooltip>
      <DropdownMenuContent align="start" className="new-chat-project-menu">
        <div className="p-2">
          <Input
            value={projectPickerSearch}
            onChange={(event) => setProjectPickerSearch(event.target.value)}
            placeholder="搜索项目"
            className="h-8 text-xs"
          />
        </div>
        <div className="max-h-[260px] overflow-y-auto custom-scrollbar px-1 pb-1">
          {filteredProjectGroups.map((group) => (
            <TextTooltip key={group.id} content={group.path} side="right">
              <button
                type="button"
                className="new-chat-project-option"
                onClick={() => {
                  setSelectedProjectId(group.id);
                  setProjectPickerSearch('');
                  setProjectPickerOpen(false);
                }}
              >
                <FolderOpen className="h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1 text-left">
                  <span className="block truncate text-sm">{group.label}</span>
                  <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                    {group.path}
                  </span>
                </span>
                {selectedProjectId === group.id && <Check className="h-4 w-4 shrink-0" />}
              </button>
            </TextTooltip>
          ))}
          {filteredProjectGroups.length === 0 && (
            <div className="px-3 py-3 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              没有匹配的项目
            </div>
          )}
        </div>
        <div className="border-t px-1 py-1" style={{ borderColor: 'var(--border)' }}>
          <button
            type="button"
            className="new-chat-project-option"
            onClick={() => {
              setProjectPickerOpen(false);
              openProjectFolderDialog('create');
            }}
          >
            <FolderPlus className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-sm">新建文件夹</span>
              <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                创建空项目
              </span>
            </span>
          </button>
          <button
            type="button"
            className="new-chat-project-option"
            onClick={() => {
              setProjectPickerOpen(false);
              openProjectFolderDialog('existing');
            }}
          >
            <FolderOpen className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1 text-left">
              <span className="block truncate text-sm">使用现有文件夹</span>
              <span className="block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                添加为项目
              </span>
            </span>
          </button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );

  const handleNewConversation = () => {
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    clearCurrentConversation();
  };
  const sendNextQueuedMessage = useCallback(async (conversationId: string): Promise<boolean> => {
    const nextMessage = queuedMessagesRef.current.find((message) =>
      message.conversationId === conversationId && message.content.trim()
      && (
        !message.blockingRunIds?.length
        || streamManager.areRunsInactive(message.blockingRunIds)
      )
    );
    if (!nextMessage) {
      updateQueuedMessages((messages) =>
        messages.filter((message) => message.conversationId !== conversationId || message.content.trim())
      );
      return false;
    }

    updateQueuedMessages((messages) => messages.filter((message) => message.id !== nextMessage.id));
    autoScrollRef.current = true;
    await startStreaming(
      conversationId,
      {
        ...nextMessage.request,
        content: nextMessage.content,
      },
      nextMessage.content,
      nextMessage.nodeId || undefined,
      nextMessage.anchorNodeId ?? nextMessage.nodeId ?? null,
    );
    return true;
  }, [startStreaming, updateQueuedMessages]);

  const handleUpdateQueuedMessage = useCallback((id: string, content: string) => {
    updateQueuedMessages((messages) =>
      messages.map((message) => message.id === id ? { ...message, content } : message)
    );
  }, [updateQueuedMessages]);

  const handleDeleteQueuedMessage = useCallback((id: string) => {
    updateQueuedMessages((messages) => messages.filter((message) => message.id !== id));
  }, [updateQueuedMessages]);

  if (!conversationSyncCoordinatorRef.current) {
    conversationSyncCoordinatorRef.current = new ConversationSyncCoordinator();
  }
  conversationSyncCoordinatorRef.current.setOperations({
    refreshMessages,
    refreshBranches,
    refreshTranscript: refreshVisibleTranscriptSnapshot,
    loadConversations,
    loadTree,
    refreshTaskState,
  });
  const scheduleConversationSync = useCallback((
    conversationId: string,
    request: ConversationSyncRequest,
  ) => (
    conversationSyncCoordinatorRef.current?.schedule(conversationId, request)
    ?? Promise.resolve({ messagesConfirmed: false })
  ), []);
  scheduleConversationSyncRef.current = scheduleConversationSync;

  useEffect(() => {
    return streamManager.onTranscriptPatch((patch, sourceRun) => {
      const visible = getCurrentVisibleTranscriptTip();
      if (!visible || visible.conversationId !== patch.conversation_id) return;
      if (!shouldPatchRunIntoMainConversation(sourceRun)) return;
      if (visible.tipNodeId !== patch.node_id) {
        useConversationStore.getState().setCurrentNodeIdLocal(patch.node_id);
        void (async () => {
          await loadTranscriptSnapshot(patch.conversation_id, patch.node_id);
          const current = getCurrentVisibleTranscriptTip();
          if (!current || current.conversationId !== patch.conversation_id || current.tipNodeId !== patch.node_id) return;
          const result = applyTranscriptPatch(transcriptStateRef.current, patch);
          if (result.status !== 'ignored') {
            transcriptStateRef.current = result.state;
            setTranscriptItems(result.state.items);
          }
          if (result.status === 'snapshot_needed') {
            void scheduleConversationSync(patch.conversation_id, {
              reason: 'transcript-patch-calibration',
              include: ['transcript'],
              messageRetries: 0,
            });
          }
        })();
        return;
      }
      const result = applyTranscriptPatch(transcriptStateRef.current, patch);
      if (result.status !== 'ignored') {
        transcriptStateRef.current = result.state;
        setTranscriptItems(result.state.items);
      }
      if (result.status === 'snapshot_needed') {
        void scheduleConversationSync(patch.conversation_id, {
          reason: 'transcript-patch-calibration',
          include: ['transcript'],
          messageRetries: 0,
        });
      }
    });
  }, [loadTranscriptSnapshot, scheduleConversationSync]);

  const handleCopyTranscriptItem = useCallback(async (_item: TranscriptItem, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedTranscriptRunId(_item.id);
      window.setTimeout(() => setCopiedTranscriptRunId(null), 1600);
    } catch (error) {
      console.error('Failed to copy transcript item:', error);
    }
  }, []);

  const runPlanActionStream = useCallback(async (
    action: 'answer' | 'approve' | 'reject',
    item: PlanApprovalItem | PlanQuestionItem,
    openStream: (conversationId: string, signal: AbortSignal) => AsyncGenerator<unknown, void>,
  ) => {
    const conversationId = item.conversation_id || currentConversation?.id;
    if (!conversationId || !item.node_id || !item.plan_id) {
      setPlanError('计划动作缺少必要上下文，无法继续。');
      return;
    }
    setPlanActionPending(action);
    setPlanError(null);
    try {
      await streamManager.startPlanActionStream(
        conversationId,
        item.node_id,
        action,
        (signal) => openStream(conversationId, signal) as AsyncGenerator<any, void>,
      );
    } catch (error) {
      void scheduleConversationSync(conversationId, {
        reason: 'plan-action-failed-calibration',
        include: ['transcript'],
        messageRetries: 0,
      });
      setPlanError(error instanceof Error ? error.message : '计划动作执行失败');
    } finally {
      setPlanActionPending(null);
    }
  }, [currentConversation?.id, scheduleConversationSync]);

  const handleApprovePlan = useCallback(async (item: PlanApprovalItem) => {
    await runPlanActionStream(
      'approve',
      item,
      (conversationId, signal) => messageApi.approvePlan(conversationId, item.plan_id, { signal }),
    );
  }, [runPlanActionStream]);

  const handleRejectPlan = useCallback(async (item: PlanApprovalItem) => {
    await runPlanActionStream(
      'reject',
      item,
      (conversationId, signal) => messageApi.rejectPlan(conversationId, item.plan_id, '', { signal }),
    );
  }, [runPlanActionStream]);

  const handleAnswerPlanQuestion = useCallback(async (item: PlanQuestionItem, answer: string) => {
    await runPlanActionStream(
      'answer',
      item,
      (conversationId, signal) => messageApi.answerPlanQuestion(conversationId, item.plan_id, answer, { signal }),
    );
  }, [runPlanActionStream]);

  const runToolApprovalAction = useCallback(async (
    action: 'approve' | 'reject',
    item: ToolApprovalItem,
  ) => {
    const conversationId = item.conversation_id || currentConversation?.id;
    if (!conversationId || !item.node_id) {
      setToolApprovalError('工具审批缺少会话或节点信息，无法继续。');
      return;
    }
    if (!item.tool_call_id) {
      setToolApprovalError('工具审批缺少 tool_call_id，无法继续。');
      return;
    }
    if (!item.run_id) {
      setToolApprovalError('工具审批缺少 run_id，无法接收后续流。');
      return;
    }
    setToolApprovalPending(`${item.id}:${action}`);
    setToolApprovalError(null);
    try {
      if (action === 'approve') {
        await messageApi.approveTool(conversationId, item.tool_call_id, item.node_id);
      } else {
        await messageApi.rejectTool(conversationId, item.tool_call_id, item.node_id);
      }
      void streamManager.resumeStream(
        conversationId,
        item.node_id,
        item.run_id,
        item.node_id,
        'chat',
        { anchorUntilTargetLands: false },
      ).catch((error) => {
        setToolApprovalError(error instanceof Error ? error.message : '工具审批后接收流失败');
      });
    } catch (error) {
      setToolApprovalError(error instanceof Error ? error.message : '工具审批失败');
    } finally {
      setToolApprovalPending(null);
    }
  }, [currentConversation?.id]);

  const handleApproveTool = useCallback(async (item: ToolApprovalItem) => {
    await runToolApprovalAction('approve', item);
  }, [runToolApprovalAction]);

  const handleRejectTool = useCallback(async (item: ToolApprovalItem) => {
    await runToolApprovalAction('reject', item);
  }, [runToolApprovalAction]);

  const handleEditUserMessage = useCallback(async (item: UserMessageItem, text: string) => {
    if (!isTranscriptItemOnCurrentBranch(item, currentConversation?.id ?? null, currentBranchNodeIds)) return;
    const parentNodeId = item.parent_node_id;
    if (!parentNodeId) return;
    const inheritedToolPermissionMode = liveBranchToolPermissionMode ?? currentBranchToolPermissionMode ?? null;
    const attachmentRefs = getEditableUserMessageAttachmentRefs(item);
    const protectedAttachmentNames = [
      ...attachmentRefs.importFiles,
      ...attachmentRefs.imageRefs.map((file) => file.filename),
    ];
    setEditValue(text);
    setEditTargetNodeId(parentNodeId);
    setEditToolPermissionMode(inheritedToolPermissionMode);
    setEditReturnNodeId(selectedBranchTipId);
    setEditProtectedAttachmentNames(protectedAttachmentNames);
    setAttachedFiles(attachmentRefs.importFiles);
    setAttachedImageRefs(attachmentRefs.imageRefs);
    await switchNode(parentNodeId);
  }, [
    currentBranchNodeIds,
    currentBranchToolPermissionMode,
    currentConversation?.id,
    liveBranchToolPermissionMode,
    selectedBranchTipId,
    switchNode,
  ]);

  const handleCancelEdit = useCallback(async () => {
    const returnNodeId = editReturnNodeId;
    const conversationId = currentConversation?.id;
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    setAttachedFiles([]);
    setAttachedImageRefs([]);
    if (conversationId && returnNodeId) {
      await switchNode(returnNodeId);
    }
  }, [currentConversation?.id, editReturnNodeId, switchNode]);

  const handleDeleteUserMessage = useCallback(async (item: UserMessageItem) => {
    if (!isTranscriptItemVisibleNow(item, currentConversation?.id ?? null, selectedBranchTipId)) return;
    const nodeId = getTranscriptItemNodeId(item);
    if (!nodeId || !currentConversation?.id) return;
    if (!window.confirm('确定删除这条消息及其后续分支？')) return;
    await deleteNode(nodeId);
    await refreshVisibleTranscriptSnapshot(currentConversation.id);
  }, [currentConversation?.id, deleteNode, refreshVisibleTranscriptSnapshot, selectedBranchTipId]);

  const handleStopStreaming = useCallback(() => {
    if (currentConversation?.id) {
      const conversationId = currentConversation.id;
      updateQueuedMessages((messages) => messages.filter((message) =>
        message.conversationId !== conversationId
        || (
          message.nodeId != null
          && message.nodeId !== selectedBranchTipId
        )
      ));
    }
    const conversationId = currentConversation?.id;
    void (async () => {
      if (conversationId) {
        const localStops = currentBranchStoppableRunIds.map((runId) => streamManager.stopRun(runId));
        await Promise.allSettled(localStops);
        setBackendActiveStreamConversationCounts((current) => {
          const activeCount = streamManager.getConversationStates(conversationId)
            .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval' || state.status === 'stopping')
            .length;
          const next = new Map(current);
          if (activeCount > 0) next.set(conversationId, activeCount);
          else next.delete(conversationId);
          return next;
        });
        await scheduleConversationSync(conversationId, {
          reason: 'stop-streaming',
          include: ['taskState'],
        });
      }
    })();
  }, [currentBranchStoppableRunIds, currentConversation?.id, scheduleConversationSync, selectedBranchTipId, updateQueuedMessages]);

  // 全局注册一次：任意对话的流结束（completed/error/stopped）时，
  // 按 transcript patch 完成状态清理 StreamManager 中该对话的临时状态。
  // 不依赖当前查看的是哪个对话，因此切走的对话流完成也能正确落地。
  useEffect(() => {
    const unsubscribe = streamManager.onFinish(async ({ conversationId: finishedId, runId, drained, nodeId, targetNodeId, controller }) => {
      const finishedRun = streamManager.getConversationStates(finishedId).find((state) => state.runId === runId);
      const shouldPatchMainConversation = finishedRun ? shouldPatchRunIntoMainConversation(finishedRun) : true;
      void scheduleConversationSync(finishedId, {
        reason: 'stream-finished-task-state',
        include: ['taskState'],
      });
      const needsTranscriptCalibration = shouldPatchMainConversation && !drained;

      if (!shouldPatchMainConversation || (!targetNodeId && !nodeId)) {
        if (!finishedRun || !shouldRenderRunDraft(finishedRun)) {
          streamManager.cleanupIfController(finishedId, controller, runId);
        }
        const include = finishedId === currentConversationIdRef.current
          ? ['conversations', 'tree'] as const
          : ['conversations'] as const;
        await scheduleConversationSync(finishedId, {
          reason: 'stream-finished-non-main',
          include: [...include],
        });
        await sendNextQueuedMessage(finishedId);
        return;
      }

      if (needsTranscriptCalibration) {
        await scheduleConversationSync(finishedId, {
          reason: 'stream-attach-calibration',
          include: ['transcript', 'tree'],
          messageRetries: 0,
        });
      } else {
        await scheduleConversationSync(finishedId, {
          reason: 'stream-finished-usage',
          include: ['tree'],
        });
      }
      streamManager.cleanupIfController(finishedId, controller, runId);
      await sendNextQueuedMessage(finishedId);
    });
    return unsubscribe;
  }, [
    scheduleConversationSync,
    sendNextQueuedMessage,
  ]);

  useLayoutEffect(() => {
    if (currentBranchHasStreamingChat && autoScrollRef.current) {
      scrollToBottom();
    }
  }, [currentBranchHasStreamingChat, scrollToBottom, transcriptItems]);

  useEffect(() => {
    loadConversations();
  }, []);

  useEffect(() => {
    const updateLocalStreamingIds = () => {
      const counts = new Map<string, number>();
      for (const conversationId of streamManager.getStreamingConversationIds()) {
        const count = streamManager.getConversationStates(conversationId)
          .filter((state) => state.status === 'streaming' || state.status === 'waiting_approval')
          .length;
        if (count > 0) counts.set(conversationId, count);
      }
      setLocalStreamingConversationCounts(counts);
    };
    updateLocalStreamingIds();
    return streamManager.subscribe(updateLocalStreamingIds);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const scheduleNextSync = (activeStreamCount: number) => {
      clearTimer();
      const delay = getActiveStreamPollingDelay({
        activeStreamCount,
        documentHidden: document.hidden,
      });
      if (delay === null || cancelled) return;
      timer = window.setTimeout(syncBackendActiveStreams, delay);
    };

    const syncBackendActiveStreams = async () => {
      if (document.hidden) {
        scheduleNextSync(0);
        return;
      }
      try {
        const activeStreams = await messageApi.getAllActiveStreams();
        if (cancelled) return;
        const counts = new Map<string, number>();
        for (const item of activeStreams) {
          if (!item.conversation_id) continue;
          counts.set(item.conversation_id, (counts.get(item.conversation_id) ?? 0) + 1);
          if (
            item.done
            || item.conversation_id !== currentConversationIdRef.current
            || item.kind !== 'chat'
            || (!item.run_id && !item.target_node_id && !item.node_id)
          ) {
            continue;
          }
          void streamManager.resumeStream(
            item.conversation_id,
            item.target_node_id ?? item.node_id,
            item.run_id ?? undefined,
            item.anchor_node_id ?? null,
            item.kind ?? 'chat',
          ).catch(() => {});
        }
        setBackendActiveStreamConversationCounts(counts);
        scheduleNextSync(activeStreams.filter((item) => !item.done).length);
      } catch {
        if (!cancelled) setBackendActiveStreamConversationCounts(new Map());
        scheduleNextSync(0);
      }
    };

    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearTimer();
        return;
      }
      void syncBackendActiveStreams();
    };

    void syncBackendActiveStreams();
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      cancelled = true;
      clearTimer();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  const handleSelectConversation = async (id: string) => {
    if (currentConversation && historyRef.current) {
      setScrollPositions(prev => ({
        ...prev,
        [currentConversation.id]: historyRef.current!.scrollTop
      }));
    }
    pendingScrollId.current = id;
    setEditValue(null);
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    const selected = conversations.find((conversation) => conversation.id === id);
    if (selected?.workspace?.cwd) {
      const group = allProjectGroups.find((item) => item.path === selected.workspace?.cwd);
      if (group) setSelectedProjectId(group.id);
    }
    await selectConversation(id);
  };

  useLayoutEffect(() => {
    if (pendingScrollId.current && historyRef.current && currentConversation?.id === pendingScrollId.current) {
      const savedPosition = scrollPositions[pendingScrollId.current];
      if (savedPosition !== undefined) {
        historyRef.current.scrollTop = savedPosition;
      } else {
        historyRef.current.scrollTop = historyRef.current.scrollHeight;
      }
      pendingScrollId.current = null;
      autoScrollRef.current = isAtBottom();
    }
  }, [currentConversation, isAtBottom, transcriptItems, scrollPositions]);

  // 从树视图双击跳转：等待消息渲染后滚动到目标节点
  useEffect(() => {
    if (!pendingScrollNodeId || chatViewMode !== 'chat') return;
    const idx = transcriptItems.findIndex((item) => getTranscriptItemNodeId(item) === pendingScrollNodeId);
    if (idx === -1) return;
    const tryScroll = () => {
      const el = findTranscriptAnchorElement(historyRef.current, {
        nodeId: pendingScrollNodeId,
        legacyIndex: idx,
      });
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        clearPendingScroll();
      } else {
        requestAnimationFrame(tryScroll);
      }
    };
    requestAnimationFrame(tryScroll);
  }, [pendingScrollNodeId, transcriptItems, chatViewMode, clearPendingScroll]);

  const handleExportMarkdown = () => {
    if (!transcriptItems.length || !currentConversation) return;
    const title = currentConversation.title || '未命名对话';
    const lines: string[] = [];
    lines.push(`# ${title}`);
    lines.push('');
    for (const item of transcriptItems) {
      if (item.type !== 'user_message' && item.type !== 'assistant_answer') continue;
      const displayContent = item.type === 'user_message' ? getUserDisplayContent(item) : item.content;
      const importFiles = item.type === 'user_message' ? getUserAttachmentNames(item) : [];
      const roleLabel = item.type === 'user_message' ? '**User**' : '**Assistant**';
      lines.push(`### ${roleLabel}`);
      lines.push('');
      for (const filename of importFiles) {
        lines.push(`- 附件: ${filename}`);
      }
      if (importFiles.length > 0) lines.push('');
      lines.push(displayContent);
      lines.push('');
      lines.push('---');
      lines.push('');
    }
    const md = lines.join('\n');
    const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${title}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleFilesPicked = async (files: File[]) => {
    let convId = currentConversation?.id;
    if (!convId) {
      const newConv = await createConversation({
        title: files[0]?.name?.slice(0, 20) || 'New',
        workspace: workspaceForCreateRequest(),
        multi_agent_mode: newConversationMultiAgentMode,
      });
      if (!newConv) return;
      convId = newConv.id;
    }
    for (const file of files) {
      const mutation = importAssetMutationOwner.begin(convId, file.name);
      try {
        const res = await importAssetMutationQueue.run(
          convId,
          file.name,
          () => conversationApi.uploadImport(convId, file),
        );
        const claimed = importAssetMutationOwner.claim(mutation, convId, res.filename);
        if (!claimed || !importAssetMutationOwner.owns(claimed)) continue;
        if (useConversationStore.getState().currentConversation?.id !== convId) continue;
        if (res.kind === 'image') {
          importAssetPreviewCache.installFile(convId, res.filename, file);
          setAttachedImageRefs(prev => prev.some(ref => ref.filename === res.filename)
            ? prev
            : [...prev, { filename: res.filename, mime_type: res.mime_type ?? file.type }]);
        } else {
          setAttachedFiles(prev => prev.includes(res.filename) ? prev : [...prev, res.filename]);
        }
      } catch (err: any) {
        console.error('Upload failed:', err?.response?.data?.detail || err.message);
      }
    }
  };

  const handleRemoveFile = async (filename: string) => {
    if (!currentConversation) return;
    const conversationId = currentConversation.id;
    const mutation = importAssetMutationOwner.begin(conversationId, filename);
    importAssetPreviewCache.remove(conversationId, filename);
    const isReferencedByHistory = transcriptItems.some((item) =>
      item.type === 'user_message' && userMessageItemReferencesAttachment(item, filename)
    );
    const isProtectedEditAttachment = editProtectedAttachmentNames.includes(filename);
    try {
      if (!isReferencedByHistory && !isProtectedEditAttachment) {
        await importAssetMutationQueue.run(
          conversationId,
          filename,
          () => conversationApi.deleteImport(conversationId, filename),
        );
      }
    } catch (_) {}
    if (!importAssetMutationOwner.owns(mutation)
        || useConversationStore.getState().currentConversation?.id !== conversationId) return;
    setAttachedFiles(prev => prev.filter(f => f !== filename));
    setAttachedImageRefs(prev => prev.filter(ref => ref.filename !== filename));
    setEditProtectedAttachmentNames(prev => prev.filter(name => name !== filename));
  };

  const getImportAssetPreviewUrl = (filename: string, conversationId = currentConversation?.id) => {
    if (!conversationId) return '';
    return importAssetPreviewCache.peek(conversationId, filename) ?? '';
  };

  const handlePreviewImage = async (filename: string) => {
    const conversationId = currentConversation?.id;
    if (!conversationId) return;
    const url = getImportAssetPreviewUrl(filename, conversationId)
      || await importAssetPreviewCache.load(conversationId, filename);
    if (!url) return;
    if (useConversationStore.getState().currentConversation?.id !== conversationId) return;
    setPreviewImage({ name: filename, url });
  };

  const handleSend = async (
    val: string,
    modelId?: string,
    providerId?: string,
    toolPermissionMode?: ToolPermissionMode,
    promptId?: string | null,
    promptMode?: 'override' | 'append',
    multiAgentMode?: MultiAgentMode,
  ) => {
    if (!val.trim()) return;
    autoScrollRef.current = true;
    requestAnimationFrame(scrollToBottom);

    let conversationId = currentConversation?.id;
    const importFiles = attachedFiles.map(filename => ({ filename }));
    const imageRefs = attachedImageRefs.map(({ filename, mime_type }) => ({ filename, mime_type }));
    const clearAttachments = () => {
      if (importFiles.length > 0) setAttachedFiles([]);
      if (imageRefs.length > 0) setAttachedImageRefs([]);
    };
    const { currentReasoningEffort, currentThinkingEnabled } = useModelStore.getState();
    const buildRequest = (parentNodeId: string): SendMessageRequest => ({
      content: val,
      parent_node_id: parentNodeId,
      focus_new_node: true,
      model_id: modelId,
      provider_id: providerId,
      reasoning_effort: currentReasoningEffort,
      thinking_enabled: currentThinkingEnabled,
      tool_permission_mode: toolPermissionMode ?? (editTargetNodeId ? editToolPermissionMode ?? undefined : undefined),
      task_context_mode: taskContextMode,
      import_files: importFiles.length > 0 ? importFiles : undefined,
      image_refs: imageRefs.length > 0 ? imageRefs : undefined,
    });
    let sendNodeId = resolveSendNodeId({
      editTargetNodeId,
      currentNodeId,
      conversationCurrentNodeId: currentConversation?.current_node_id,
    });
    const slashMatch = slashRegistry.match(val);
    const slashCommand = slashMatch?.command ?? null;
    let streamNodeId = resolveSlashStreamNodeId({
      sendNodeId,
      streamTargetPolicy: slashCommand?.stream_target_policy,
    });
    let request = sendNodeId ? buildRequest(sendNodeId) : null;

    if (conversationId && request && shouldQueueForMainThread({ currentBranchHasStreamingChat, slashCommand })) {
      const queuedConversationId = conversationId;
      const queuedRequest = request;
      clearAttachments();
      setEditTargetNodeId(null);
      setEditToolPermissionMode(null);
      setEditReturnNodeId(null);
      setEditProtectedAttachmentNames([]);
      updateQueuedMessages((messages) => [
        ...messages,
        {
          id: createQueuedMessageId(),
          conversationId: queuedConversationId,
          nodeId: streamNodeId ?? null,
          anchorNodeId: sendNodeId ?? null,
          blockingRunIds: currentBranchStreamingRunIds,
          content: val,
          request: queuedRequest,
        },
      ]);
      return;
    }

    if (!conversationId) {
      const newConv = await createConversation({
        title: val.slice(0, 20),
        prompt_id: promptId || undefined,
        prompt_mode: promptId ? promptMode : undefined,
        workspace: workspaceForCreateRequest(),
        multi_agent_mode: multiAgentMode ?? newConversationMultiAgentMode,
      });
      if (!newConv) {
        console.error('Failed to create conversation');
        return;
      }
      conversationId = newConv.id;
      sendNodeId = resolveSendNodeId({
        editTargetNodeId: null,
        currentNodeId: null,
        conversationCurrentNodeId: newConv.current_node_id,
      });
      streamNodeId = resolveSlashStreamNodeId({
        sendNodeId,
        streamTargetPolicy: slashCommand?.stream_target_policy,
      });
      request = sendNodeId ? buildRequest(sendNodeId) : null;
    }

    if (!conversationId || !sendNodeId || !request) {
      console.error('无法确定消息父节点');
      return;
    }

    clearAttachments();
    setEditTargetNodeId(null);
    setEditToolPermissionMode(null);
    setEditReturnNodeId(null);
    setEditProtectedAttachmentNames([]);
    // 第三个参数是乐观渲染的用户气泡文本（显示用户输入的原文）。
    // 推理设置从 modelStore 的当前值读取（已确认值），随请求透传。
    void startStreaming(
      conversationId,
      request,
      val,
      streamNodeId,
      sendNodeId ?? null,
    ).catch((err) => {
      console.error('发送失败:', err);
    });
  };

  const handleJumpToMessage = (target: TranscriptScrollTarget) => {
    const element = findTranscriptAnchorElement(historyRef.current, target);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  const parseFileMention = (content: string): { fileNames: string[]; cleanContent: string } | null => {
    const match = content.match(/^'''USER MENTIONED FILES:\s+(.*?)\s+'''\n\n[\s\S]*?\n---\n\n/s);
    if (!match) return null;
    const fileNames = match[1].split(/\s+/).filter(Boolean);
    const cleanContent = content.slice(match[0].length);
    return { fileNames, cleanContent };
  };

  const getUserImportFileNames = (message: UserMessageItem): string[] => {
    const structured = (message.import_files ?? [])
      .map((file) => file.filename)
      .filter(Boolean);
    if (structured.length > 0) return structured;
    return parseFileMention(message.content)?.fileNames ?? [];
  };

  const getUserImageRefs = (message: UserMessageItem): Array<{ filename: string; mime_type?: string }> => {
    return (message.image_refs ?? [])
      .filter((file) => Boolean(file.filename))
      .map((file) => ({ filename: file.filename, mime_type: file.mime_type ?? undefined }));
  };

  const getUserAttachmentNames = (message: UserMessageItem): string[] => {
    return [
      ...getUserImportFileNames(message),
      ...getUserImageRefs(message).map(file => file.filename),
    ];
  };

  const getUserDisplayContent = (message: UserMessageItem): string => {
    return parseFileMention(message.content)?.cleanContent ?? message.content;
  };

  const outline = transcriptItems
    .map((item, index) => ({ ...item, originalIndex: index }))
    .filter((item): item is UserMessageItem & { originalIndex: number } => item.type === 'user_message')
    .map((m) => {
      const clean = getUserDisplayContent(m);
      return {
        text: clean.slice(0, 20) + (clean.length > 20 ? '...' : ''),
        originalIndex: m.originalIndex,
        messageId: m.id,
        nodeId: m.node_id,
      };
    });

  const renderTranscriptItem = (item: TranscriptItem, defaultItem: React.ReactNode) => {
    const nodeId = getTranscriptItemNodeId(item);
    return (
      <div
        role="presentation"
        className="w-full"
        data-transcript-item-id={item.id}
        data-transcript-message-id={getTranscriptItemMessageId(item) || undefined}
        data-transcript-node-id={nodeId || undefined}
      >
        {defaultItem}
      </div>
    );
  };

  const getSideRunGroupLabel = (kind: string): string => {
    if (kind === 'side_question') return '旁路问题';
    if (kind === 'subagent') return '后台分支';
    if (kind === 'command') return '后台命令';
    if (kind === 'workflow') return 'Workflow';
    if (kind === 'direct_response') return '命令响应';
    return kind;
  };

  const getSideRunTitle = (run: StreamState): string => {
    const metadata = run.metadata || {};
    const candidates = [
      run.summary,
      typeof metadata.command === 'string' ? metadata.command : '',
      typeof metadata.workflow_step_name === 'string' ? metadata.workflow_step_name : '',
      typeof metadata.agent_name === 'string' ? metadata.agent_name : '',
      typeof metadata.original_slash_input === 'string' ? metadata.original_slash_input : '',
      typeof metadata.delegated_task === 'string' ? metadata.delegated_task : '',
      run.pendingUserMessage,
    ];
    return candidates.find((value) => typeof value === 'string' && value.trim().length > 0)?.trim()
      || `${getSlashRunLabel(run.kind, run.pendingUserMessage)} · ${run.runId.slice(0, 12)}`;
  };

  const getSideRunStatusText = (run: StreamState): string => {
    if (run.status === 'streaming') return '运行中';
    if (run.status === 'waiting_approval') return '等待审批';
    if (run.status === 'completed') return '已完成';
    if (run.status === 'error') return '出错';
    if (run.status === 'stopped') return '已停止';
    return run.status;
  };

  const getSideRunStatusColor = (run: StreamState): string => {
    if (run.status === 'completed') return 'var(--fg-tertiary)';
    if (run.status === 'error') return 'var(--destructive)';
    if (run.status === 'stopped') return 'var(--fg-tertiary)';
    if (run.status === 'waiting_approval') return 'var(--icon-accent)';
    return 'var(--icon-accent)';
  };

  const renderSideRunActions = (draft: SideRunDraft) => (
    <>
      {(draft.run.status === 'streaming' || draft.run.status === 'waiting_approval') && (
        <TextTooltip content="停止">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="app-run-action-button"
            onClick={(event) => {
              event.stopPropagation();
              void streamManager.stopRun(draft.run.runId);
            }}
            aria-label="停止"
          >
            <Square className="h-3.5 w-3.5" />
          </Button>
        </TextTooltip>
      )}
      <TextTooltip content="关闭">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="app-run-action-button"
          onClick={(event) => {
            event.stopPropagation();
            if (selectedSideRunId === draft.run.runId) setSelectedSideRunId(null);
            if (currentConversation?.id) hideSideRun(currentConversation.id, draft.run.runId);
            streamManager.cleanupRun(draft.run.runId);
          }}
          aria-label="关闭"
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </TextTooltip>
    </>
  );

  const renderTaskPanel = () => (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar">
      <div className="flex h-8 shrink-0 items-center justify-between px-1">
        <span className="text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>
          任务上下文
        </span>
        <div className="flex items-center gap-2">
          <span className="text-xs" style={{ color: 'var(--fg-secondary)' }}>
            {taskContextMode === 'attached' ? '接入' : '隔离'}
          </span>
          <TextTooltip content={taskContextMode === 'attached' ? '下一条消息接入任务上下文' : '下一条消息不接入任务上下文'}>
            <Switch
              size="sm"
              checked={taskContextMode === 'attached'}
              onCheckedChange={(checked) => setTaskContextMode(checked ? 'attached' : 'detached')}
              aria-label="切换下一条消息的任务上下文"
            />
          </TextTooltip>
        </div>
      </div>
      {!taskPanelItem && (
        <div className="rounded-lg border px-3 py-4 text-sm" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
          当前对话暂无任务。
        </div>
      )}
      {taskPanelItem && (() => {
        const item = taskPanelItem;
        const statusColor = item.task.status === 'blocked'
          ? 'var(--destructive)'
          : 'var(--icon-accent)';
        return (
          <div
            key="active-task"
            className="app-run-list-row cursor-default p-3"
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full" style={{ color: statusColor }}>
                {item.running || item.stopping ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : item.task.status === 'blocked' ? (
                  <X className="h-3.5 w-3.5" />
                ) : (
                  <span className="h-2 w-2 rounded-full" style={{ background: statusColor }} />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold" style={{ color: 'var(--fg-secondary)' }}>
                  {item.title}
                </div>
              </div>
              <span className="shrink-0 text-xs" style={{ color: statusColor }}>
                {item.statusLabel}
              </span>
            </div>
            <div className="mt-2 flex flex-col gap-1 pl-7 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              <div className="truncate" style={{ color: 'var(--fg-secondary)' }}>
                {item.progressText}
              </div>
              {item.task.detail && (
                <div className="line-clamp-2">{item.task.detail}</div>
              )}
              {item.steps.length > 0 && (
                <div className="mt-1 flex flex-col gap-1.5">
                  {item.steps.map((step) => {
                    const stepColor = step.step.status === 'blocked'
                      ? 'var(--destructive)'
                      : step.step.status === 'completed'
                        ? 'var(--fg-tertiary)'
                        : 'var(--icon-accent)';
                    return (
                      <div key={step.step.position} className="flex min-w-0 items-start gap-2">
                        <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center" style={{ color: stepColor }}>
                          {step.step.status === 'completed' ? (
                            <Check className="h-3 w-3" />
                          ) : step.running ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : step.step.status === 'blocked' ? (
                            <X className="h-3 w-3" />
                          ) : (
                            <span className="h-1.5 w-1.5 rounded-full" style={{ background: stepColor }} />
                          )}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-center gap-2">
                            <span className="min-w-0 flex-1 truncate" style={{ color: 'var(--fg-secondary)' }}>
                              {step.step.position}. {step.title}
                            </span>
                            <span className="shrink-0" style={{ color: stepColor }}>
                              {step.statusLabel}
                            </span>
                          </div>
                          {step.step.detail && step.step.detail !== step.step.title && (
                            <div className="mt-0.5 line-clamp-2">{step.step.detail}</div>
                          )}
                          {step.step.evidence_summary && (
                            <div className="mt-0.5 truncate">{step.step.evidence_summary}</div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        );
      })()}
    </div>
  );

  const renderCommandRunBody = (run: StreamState) => {
    const shell = run.metadata?.shell && typeof run.metadata.shell === 'object'
      ? run.metadata.shell as Record<string, unknown>
      : null;
    const commandLanguage = typeof shell?.highlighter_language === 'string'
      ? shell.highlighter_language
      : 'bash';
    const output = [
      run.command.stdout ? `$ stdout\n${run.command.stdout}` : '',
      run.command.stderr ? `$ stderr\n${run.command.stderr}` : '',
    ].filter(Boolean).join('\n');
    const command = run.command.command
      || (typeof run.metadata.command === 'string' ? run.metadata.command : '')
      || run.summary
      || run.runId;
    const cwd = run.command.cwd || (typeof run.metadata.cwd === 'string' ? run.metadata.cwd : '');
    return (
      <div className="flex flex-col gap-2">
        <div className="rounded border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
          <div className="truncate">{command}</div>
          {cwd && <div className="truncate">cwd: {cwd}</div>}
          {(run.command.exitCode !== null || run.command.durationSeconds !== null) && (
            <div>
              {run.command.exitCode !== null ? `exit: ${run.command.exitCode}` : ''}
              {run.command.durationSeconds !== null ? ` · ${run.command.durationSeconds}s` : ''}
            </div>
          )}
        </div>
        {output ? (
          <div className="file-preview-code-shell custom-scrollbar">
            <SyntaxHighlighter
              language={commandLanguage}
              style={oneDark}
              customStyle={{
                margin: 0,
                background: 'transparent',
                fontSize: '12px',
                lineHeight: '1.45',
              }}
              wrapLongLines
            >
              {output}
            </SyntaxHighlighter>
          </div>
        ) : run.status === 'streaming' ? (
          <div className="flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--fg-tertiary)' }}>
            <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
            <span>运行中...</span>
          </div>
        ) : null}
        {getStreamStatusLabel(run.status, run.errorMessage) && (
          <div className="text-xs text-destructive">
            {getStreamStatusLabel(run.status, run.errorMessage)}
          </div>
        )}
      </div>
    );
  };

  const renderSideRunBody = (draft: SideRunDraft) => (
    <div className="flex flex-col gap-2">
      {draft.showPendingBubble && (
        <div
          className="max-w-full rounded-lg px-3 py-2 text-sm prose prose-sm prose-invert max-w-none [&_p]:m-0"
          style={{
            background: 'linear-gradient(160deg, rgba(217,119,87,0.16), rgba(217,119,87,0.08))',
            border: '0.5px solid rgba(217,119,87,0.28)',
            color: 'var(--fg-85)',
          }}
        >
          <MarkdownView content={draft.run.pendingUserMessage || ''} />
        </div>
      )}
      {draft.run.kind === 'command' && renderCommandRunBody(draft.run)}
      {draft.showStreamBlock && draft.run.kind !== 'command' && (
        <div className="min-w-0">
          {draft.run.reasoning && (
            <ThinkingBlock reasoning={draft.run.reasoning} streaming={draft.run.status === 'streaming' && draft.run.reasoningActive} />
          )}
          {draft.run.content && (
            <div
              className="max-w-full min-w-0 rounded-lg px-3 py-2 text-sm leading-relaxed prose prose-sm max-w-none [&_p]:m-0"
              style={{ color: 'var(--fg-secondary)' }}
            >
              <MarkdownView content={draft.run.content} />
            </div>
          )}
          {!draft.run.reasoning && !draft.run.content && draft.run.status === 'streaming' && (
            <div className="flex items-center gap-2 px-3 py-2 text-sm" style={{ color: 'var(--fg-tertiary)' }}>
              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
              <span>运行中...</span>
            </div>
          )}
          {getStreamStatusLabel(draft.run.status, draft.run.errorMessage) && (
            <div className="mt-1 text-xs text-destructive">
              {getStreamStatusLabel(draft.run.status, draft.run.errorMessage)}
            </div>
          )}
        </div>
      )}
    </div>
  );

  const renderWorkflowOverview = (item: SideRunGroupItem<SideRunDraft>) => {
    const progressSteps = getWorkflowProgressSteps(item.run);
    return (
      <div className="flex flex-col gap-3">
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated-secondary, rgba(255,255,255,0.03))' }}>
          <div className="mb-2 text-xs font-semibold" style={{ color: 'var(--fg-secondary)' }}>流程进度</div>
          {progressSteps.length === 0 ? (
            <div className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>等待 workflow 事件。</div>
          ) : (
            <div className="flex flex-col gap-2">
              {progressSteps.map((step) => (
                <div key={step.key} className="flex min-w-0 items-center gap-2 text-sm">
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: step.status === 'completed' ? 'var(--fg-tertiary)' : step.status === 'error' ? 'var(--destructive)' : 'var(--icon-accent)' }}
                  />
                  <span className="min-w-0 flex-1 truncate" style={{ color: 'var(--fg-secondary)' }}>{step.label}</span>
                  <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>{step.status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)', background: 'var(--bg-elevated-secondary, rgba(255,255,255,0.03))' }}>
          <div className="mb-2 text-xs font-semibold" style={{ color: 'var(--fg-secondary)' }}>子步骤</div>
          {item.steps.length === 0 ? (
            <div className="text-sm" style={{ color: 'var(--fg-tertiary)' }}>暂无可展开的步骤详情。</div>
          ) : (
            <div className="flex flex-col gap-1.5">
              {item.steps.map((step) => (
                <button
                  key={step.run.runId}
                  type="button"
                  className="flex min-w-0 items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors"
                  style={{ borderColor: 'var(--border)', background: 'transparent', color: 'var(--fg-secondary)' }}
                  onClick={() => setSelectedSideRunId(step.run.runId)}
                >
                  <span className="min-w-0 flex-1 truncate text-sm">{getSideRunTitle(step.run)}</span>
                  <span className="text-xs" style={{ color: getSideRunStatusColor(step.run) }}>{getSideRunStatusText(step.run)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  };

  const activeRightPanelWidth = rightPanelWidth;
  const activeRightPanelConfig = RIGHT_PANEL_WIDTH;

  return (
    <div
      className={cn('flex h-full', resizingSidebar && 'is-sidebar-resizing')}
      style={{ background: 'var(--bg-surface)' }}
    >
      {/* Left conversation list (collapsible) */}
      <nav
        className={cn(
          'app-sidebar',
          sidebarCollapsed && 'app-sidebar-collapsed',
          resizingSidebar === 'left' && 'is-resizing',
        )}
        style={{ width: `${sidebarCollapsed ? 56 : leftSidebarWidth}px` }}
      >
        <div className="app-sidebar-topbar">
          {!sidebarCollapsed && (
            <TextTooltip content="新对话">
              <button
                type="button"
                className={cn('app-new-chat-action', !currentConversation && 'is-active')}
                onClick={handleNewConversation}
              >
                <Plus className="h-4 w-4 shrink-0" />
                <span>新对话</span>
              </button>
            </TextTooltip>
          )}
          <TextTooltip content={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}>
            <Button
              variant="ghost"
              size="sm"
              className="app-panel-toggle"
              onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
              aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
            >
              {sidebarCollapsed ? <PanelLeftOpen className="h-5 w-5" /> : <PanelLeftClose className="h-5 w-5" />}
            </Button>
          </TextTooltip>
        </div>

        {!sidebarCollapsed && (
          <>
            <div className="app-sidebar-search">
              <Search className="h-3.5 w-3.5 shrink-0" />
              <Input
                ref={conversationSearchInputRef}
                value={conversationSearch}
                onChange={(event) => setConversationSearch(event.target.value)}
                placeholder="搜索项目或对话"
                className="h-8 border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-0"
              />
            </div>
            <div className="app-sidebar-project-heading">项目</div>
            <div className="app-sidebar-projects custom-scrollbar">
              {projectGroups.map((group) => {
                const visible = getVisibleProjectConversations(group);
                const selectedProject = selectedProjectId === group.id && !currentConversation;
                return (
                  <div
                    key={group.id}
                    ref={(element) => {
                      if (element) projectGroupRefs.current.set(group.id, element);
                      else projectGroupRefs.current.delete(group.id);
                    }}
                    className={cn('app-project-group', draggingProjectId === group.id && 'is-dragging')}
                    onDragOver={(event) => handleProjectDragOver(event, group.id)}
                    onDrop={(event) => event.preventDefault()}
                  >
                    <TextTooltip content={group.path} side="right">
                      <button
                        type="button"
                        className={cn('app-project-row', selectedProject && 'is-active')}
                        draggable
                        onDragStart={(event) => handleProjectDragStart(event, group.id)}
                        onDragEnd={handleProjectDragEnd}
                        onClick={(event) => {
                          if (projectDragMovedRef.current) {
                            event.preventDefault();
                            return;
                          }
                          setSelectedProjectId(group.id);
                          toggleProjectCollapsed(group.id);
                        }}
                      >
                        <ChevronRight
                          className={cn('h-3.5 w-3.5 shrink-0 transition-transform', !group.isCollapsed && 'rotate-90')}
                        />
                        <FolderOpen className="h-4 w-4 shrink-0" />
                        <span className="app-project-name">{group.label}</span>
                        <span className="app-project-count">{group.conversations.length}</span>
                      </button>
                    </TextTooltip>
                    {!group.isCollapsed && (
                      <div className="app-session-list">
                        {visible.items.map((c) => {
                          const isSelected = c.id === currentConversation?.id;
                          const runningCount = activeStreamConversationCounts.get(c.id) ?? 0;
                          const isRunning = runningCount > 0;
                          return (
                            <TextTooltip key={c.id} content={c.title || '未命名'} side="right">
                              <div
                                className={cn('app-session-row', isSelected && 'is-active')}
                                onClick={() => handleSelectConversation(c.id)}
                                onMouseEnter={() => setHoveredId(c.id)}
                                onMouseLeave={() => setHoveredId(null)}
                              >
                                <span className="app-session-title">{c.title || '未命名'}</span>
                                {isRunning && (
                                  <span className="app-session-running inline-flex items-center gap-1" aria-label={`正在运行 ${runningCount} 个任务`}>
                                    <Loader2 className="h-3.5 w-3.5" />
                                    {runningCount > 1 && <span className="text-[10px] tabular-nums">{runningCount}</span>}
                                  </span>
                                )}
                                <span className="app-session-time">{formatConversationTime(c.updated_at)}</span>
                                <DropdownMenu>
                                  <DropdownMenuTrigger asChild>
                                    <Button
                                      variant="ghost"
                                      size="sm"
                                      className={cn(
                                        'app-session-more',
                                        hoveredId === c.id || isSelected ? 'opacity-100' : 'opacity-0'
                                      )}
                                      onClick={(e) => e.stopPropagation()}
                                    >
                                      <MoreHorizontal className="h-4 w-4" />
                                    </Button>
                                  </DropdownMenuTrigger>
                                  <DropdownMenuContent>
                                    <DropdownMenuItem onClick={() => handleRenameClick(c.id, c.title)}>
                                      <Pencil className="h-4 w-4 mr-2" />
                                      重命名
                                    </DropdownMenuItem>
                                    <DropdownMenuItem onClick={() => deleteConversation(c.id)}>
                                      <X className="h-4 w-4 mr-2" />
                                      删除对话
                                    </DropdownMenuItem>
                                  </DropdownMenuContent>
                                </DropdownMenu>
                              </div>
                            </TextTooltip>
                          );
                        })}
                        {visible.canExpand && (
                          <button
                            type="button"
                            className="app-sidebar-inline-action"
                            onClick={() => toggleHistoryExpanded(group.id)}
                          >
                            展开 {visible.hiddenCount} 个更多会话
                          </button>
                        )}
                        {visible.canCollapse && (
                          <button
                            type="button"
                            className="app-sidebar-inline-action"
                            onClick={() => toggleHistoryExpanded(group.id)}
                          >
                            收起
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </>
        )}

        <div className="app-sidebar-footer">
          <TextTooltip content="设置">
            <button
              type="button"
              className="app-sidebar-action"
              onClick={() => openSettings('providers')}
            >
              <Settings className="h-4 w-4 shrink-0" />
              {!sidebarCollapsed && <span>设置</span>}
            </button>
          </TextTooltip>
        </div>
        {!sidebarCollapsed && (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="调整左侧栏宽度"
            aria-valuemin={LEFT_SIDEBAR_WIDTH.minWidth}
            aria-valuemax={LEFT_SIDEBAR_WIDTH.maxWidth}
            aria-valuenow={leftSidebarWidth}
            tabIndex={0}
            className="sidebar-resize-handle sidebar-resize-handle-left"
            onPointerDown={(event) => beginSidebarResize(event, 'left')}
            onKeyDown={(event) => adjustSidebarWidthFromKeyboard(event, 'left')}
          />
        )}
      </nav>

      {/* Center: title bar + content (chat or tree) */}
      <section className="flex-1 flex flex-col overflow-hidden relative" style={{ background: 'var(--bg-surface)' }}>
        {/* Title bar with view toggle */}
        <div
          className="flex items-center justify-between p-3 sticky top-0 z-[1] min-h-[56px]"
          style={{ background: 'var(--bg-surface)', borderBottom: '0.5px solid var(--border)' }}
        >
          <span className="w-8" />
          <div className="flex items-center gap-2">
            <span className="font-semibold" style={{ color: 'var(--fg-secondary)' }}>{currentConversation?.title || '请选择对话'}</span>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 w-8 p-0"
                  onClick={toggleChatViewMode}
                >
                  {chatViewMode === 'chat' ? (
                    <Network className="h-4 w-4" />
                  ) : (
                    <MessageSquare className="h-4 w-4" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {chatViewMode === 'chat' ? '切换到树视图' : '切换到对话视图'}
              </TooltipContent>
            </Tooltip>
          </div>
          <div className="flex items-center gap-1">
            <TextTooltip content="导出为 Markdown">
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0"
                onClick={handleExportMarkdown}
                disabled={!transcriptItems.length}
                aria-label="导出为 Markdown"
              >
                <Download className="h-4 w-4" />
              </Button>
            </TextTooltip>
          </div>
        </div>

        {/* Chat view */}
        {chatViewMode === 'chat' && (
          !currentConversation && transcriptItems.length === 0 ? (
            <div className="new-chat-stage">
              <div className="new-chat-center">
                <h1 className="new-chat-title">{`我们应该在 ${newChatProjectLabel} 中做些什么？`}</h1>
                <div className="new-chat-composer-wrap">
                  <ChatInput
                    variant="composer"
                    settingsSlot={projectSettingsSlot}
                    onSend={handleSend}
                    onStop={handleStopStreaming}
                    isStreaming={currentBranchHasStreamingChat}
                    disabled={currentBranchHasStreamingChat}
                    conversationId={null}
                    editValue={editValue}
                    isEditing={Boolean(editTargetNodeId)}
                    onEditValueConsumed={() => setEditValue(null)}
                    onCancelEdit={handleCancelEdit}
                    attachedFiles={attachedFiles}
                    attachedImages={attachedImageRefs.map(ref => ({
                      filename: ref.filename,
                      url: getImportAssetPreviewUrl(ref.filename),
                    }))}
                    onFilesPicked={handleFilesPicked}
                    onRemoveFile={handleRemoveFile}
                    onPreviewImage={handlePreviewImage}
                    queuedMessages={visibleQueuedMessages}
                    onUpdateQueuedMessage={handleUpdateQueuedMessage}
                    onDeleteQueuedMessage={handleDeleteQueuedMessage}
                    toolPermissionDraft={toolPermissionDraft}
                    getToolPermissionDraft={getToolPermissionDraft}
                    onToolPermissionDraftChange={updateToolPermissionDraft}
                    pendingMultiAgentMode={newConversationMultiAgentMode}
                    onPendingMultiAgentModeChange={setNewConversationMultiAgentMode}
                  />
                </div>
              </div>
            </div>
          ) : (
            <>
              <div
                ref={historyRef}
                className={cn(
                  'chat-history-scroll w-full flex-1 overflow-y-scroll pt-4 pb-[140px] flex flex-col items-center custom-scrollbar',
                  isScrolling && 'scrollbar-visible'
                )}
                onScroll={handleScroll}
              >
                <div
                  className="w-[800px] max-w-full flex flex-col px-4"
                >
                  <TranscriptList
                    items={displayTranscriptItems}
                    isLoading={transcriptLoading}
                    transcriptError={transcriptError}
                    onCopyItem={handleCopyTranscriptItem}
                    onEditUserMessage={handleEditUserMessage}
                    onDeleteUserMessage={handleDeleteUserMessage}
                    onApprovePlan={handleApprovePlan}
                    onRejectPlan={handleRejectPlan}
                    onAnswerPlanQuestion={handleAnswerPlanQuestion}
                    onApproveTool={handleApproveTool}
                    onRejectTool={handleRejectTool}
                    planActionPending={planActionPending}
                    planError={planError}
                    toolApprovalPending={toolApprovalPending}
                    toolApprovalError={toolApprovalError}
                    renderItem={renderTranscriptItem}
                  />
                </div>
              </div>
              <div
                aria-hidden="true"
                className="pointer-events-none absolute bottom-0 left-0 right-[12px] z-[9] h-[150px]"
                style={{
                  background: 'linear-gradient(180deg, color-mix(in srgb, var(--bg-surface) 0%, transparent), var(--bg-surface) 72%)',
                }}
              />
              <footer className="absolute bottom-4 left-1/2 -translate-x-1/2 w-[800px] max-w-[calc(100%-48px)] z-10">
                <ChatInput
                  onSend={handleSend}
                  onStop={handleStopStreaming}
                  isStreaming={currentBranchHasStreamingChat}
                  disabled={currentBranchHasStreamingChat}
                  conversationId={currentConversation?.id || null}
                  editValue={editValue}
                  isEditing={Boolean(editTargetNodeId)}
                  onEditValueConsumed={() => setEditValue(null)}
                  onCancelEdit={handleCancelEdit}
                  attachedFiles={attachedFiles}
                  attachedImages={attachedImageRefs.map(ref => ({
                    filename: ref.filename,
                    url: getImportAssetPreviewUrl(ref.filename),
                  }))}
                  onFilesPicked={handleFilesPicked}
                  onRemoveFile={handleRemoveFile}
                  onPreviewImage={handlePreviewImage}
                  queuedMessages={visibleQueuedMessages}
                  onUpdateQueuedMessage={handleUpdateQueuedMessage}
                  onDeleteQueuedMessage={handleDeleteQueuedMessage}
                  toolPermissionDraft={toolPermissionDraft}
                  getToolPermissionDraft={getToolPermissionDraft}
                  onToolPermissionDraftChange={updateToolPermissionDraft}
                  pendingMultiAgentMode={newConversationMultiAgentMode}
                  onPendingMultiAgentModeChange={setNewConversationMultiAgentMode}
                />
              </footer>
            </>
          )
        )}

        {/* Tree view */}
        {chatViewMode === 'tree' && (
          <div className="flex-1 overflow-hidden">
            <TreeView />
          </div>
        )}
      </section>

      {/* Right outline (only in chat mode, collapsible) */}
      {chatViewMode === 'chat' && (
        <aside
          className={cn(
            'app-right-panel flex flex-col shrink-0 transition-[width] duration-200 overflow-hidden',
            resizingSidebar === 'right' && 'is-resizing',
          )}
          style={{
            width: `${outlineCollapsed ? 56 : activeRightPanelWidth}px`,
            background: 'var(--bg-surface)',
            borderLeft: '0.5px solid var(--border)',
          }}
        >
          {!outlineCollapsed && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="调整右侧栏宽度"
              aria-valuemin={activeRightPanelConfig.minWidth}
              aria-valuemax={activeRightPanelConfig.maxWidth}
              aria-valuenow={activeRightPanelWidth}
              tabIndex={0}
              className="sidebar-resize-handle sidebar-resize-handle-right"
              onPointerDown={(event) => beginSidebarResize(event, 'right')}
              onKeyDown={(event) => adjustSidebarWidthFromKeyboard(event, 'right')}
            />
          )}
          <div className="flex justify-between items-center p-3 sticky top-0 z-[1] min-h-[56px]"
               style={{ background: 'var(--bg-surface)' }}>
            {!outlineCollapsed && (
              <div className="flex min-w-0 items-center gap-1">
                <Button
                  variant={rightPanelView === 'outline' ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs"
                  onClick={() => setRightPanelView('outline')}
                >
                  <FileText className="h-3.5 w-3.5" />
                  大纲
                </Button>
                <Button
                  variant={rightPanelView === 'side' ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-8 min-w-0 gap-1.5 px-2 text-xs"
                  onClick={() => setRightPanelView('side')}
                  aria-label="运行"
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                  <span className="min-w-0 truncate">运行</span>
                  {sideRunTopLevelCount > 0 && (
                    <span
                      className="ml-0.5 rounded-full px-1.5 py-0.5 text-[10px]"
                      style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-secondary)' }}
                    >
                      {sideRunTopLevelCount}
                    </span>
                  )}
                </Button>
                <Button
                  variant={rightPanelView === 'tasks' ? 'secondary' : 'ghost'}
                  size="sm"
                  className="h-8 min-w-0 gap-1.5 px-2 text-xs"
                  onClick={() => setRightPanelView('tasks')}
                  aria-label="任务"
                >
                  <Check className="h-3.5 w-3.5" />
                  <span className="min-w-0 truncate">任务</span>
                  {taskPanelOpenCount > 0 && (
                    <span
                      className="ml-0.5 rounded-full px-1.5 py-0.5 text-[10px]"
                      style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-secondary)' }}
                    >
                      {taskPanelOpenCount}
                    </span>
                  )}
                </Button>
              </div>
            )}
            <TextTooltip content={outlineCollapsed ? '展开大纲' : '收起大纲'}>
              <Button
                variant="ghost"
                size="sm"
                className="app-panel-toggle"
                onClick={() => setOutlineCollapsed(!outlineCollapsed)}
                aria-label={outlineCollapsed ? '展开大纲' : '收起大纲'}
              >
                {outlineCollapsed ? <PanelRightOpen className="h-5 w-5" /> : <PanelRightClose className="h-5 w-5" />}
              </Button>
            </TextTooltip>
          </div>

          {!outlineCollapsed && (
            <>
              {rightPanelView === 'outline' ? (
                <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden custom-scrollbar">
                  {outline.map((item, idx) => (
                    <TextTooltip key={idx} content={item.text} side="left">
                      <div
                        className="flex items-center py-2 px-3 cursor-pointer rounded-lg mx-2 my-0.5 transition-colors"
                        style={{ color: 'var(--fg-85)' }}
                        onClick={() => handleJumpToMessage({
                          messageId: item.messageId,
                          nodeId: item.nodeId,
                          legacyIndex: item.originalIndex,
                        })}
                        onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
                        onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
                      >
                        <span className="truncate text-sm">{item.text}</span>
                      </div>
                    </TextTooltip>
                  ))}
                </div>
              ) : rightPanelView === 'tasks' ? (
                renderTaskPanel()
              ) : selectedSideRunItem ? (
                <div className="flex min-h-0 flex-1 flex-col">
                  <div className="flex shrink-0 items-center gap-2 px-3 pb-3">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2"
                      onClick={() => {
                        const createdByRunId = selectedSideRunItem.run.createdByRunId;
                        const parentRun = createdByRunId
                          ? activeRunStates.find((run) => run.runId === createdByRunId)
                          : null;
                        const nextRunId = parentRun && SIDE_RUN_KINDS.has(parentRun.kind)
                          ? parentRun.runId
                          : null;
                        setSelectedSideRunId(nextRunId);
                      }}
                    >
                      <ArrowLeft className="h-3.5 w-3.5" />
                    </Button>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold" style={{ color: 'var(--fg-secondary)' }}>
                        {getSideRunTitle(selectedSideRunItem.run)}
                      </div>
                      <div className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                        {getSlashRunLabel(selectedSideRunItem.run.kind, selectedSideRunItem.run.pendingUserMessage)} · {selectedSideRunItem.run.runId.slice(0, 12)}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      {renderSideRunActions(selectedSideRunItem.draft)}
                    </div>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar">
                    {selectedSideRunItem.run.kind === 'workflow' && renderWorkflowOverview(selectedSideRunItem)}
                    {selectedSideRunItem.run.kind !== 'workflow' && renderSideRunBody(selectedSideRunItem.draft)}
                  </div>
                </div>
              ) : (
                <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overflow-x-hidden px-3 pb-4 custom-scrollbar">
                  {sideRunTopLevelCount === 0 && (
                    <div className="rounded-lg border px-3 py-4 text-sm" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
                      暂无运行任务。
                    </div>
                  )}
                  {sideRunGroups.map((group) => (
                    <section key={group.kind} className="flex flex-col gap-2">
                      <div className="px-1 text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>
                        {getSideRunGroupLabel(group.kind)}
                      </div>
                      {group.runs.map((item) => (
                        <div
                          key={item.run.runId}
                          role="button"
                          tabIndex={0}
                          className="app-run-list-row p-0 text-left"
                          onClick={() => setSelectedSideRunId(item.run.runId)}
                          onKeyDown={(event) => {
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            event.preventDefault();
                            setSelectedSideRunId(item.run.runId);
                          }}
                        >
                          <div className="flex items-center gap-2 px-3 py-2">
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-sm font-semibold" style={{ color: 'var(--fg-secondary)' }}>
                                {getSideRunTitle(item.run)}
                              </div>
                              <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                                <span className="truncate">{getSlashRunLabel(item.run.kind, item.run.pendingUserMessage)}</span>
                                <span>·</span>
                                <span>{item.run.runId.slice(0, 12)}</span>
                              </div>
                            </div>
                            <span className="shrink-0 text-xs" style={{ color: getSideRunStatusColor(item.run) }}>
                              {getSideRunStatusText(item.run)}
                            </span>
                            <div className="flex shrink-0 items-center gap-1">
                              {renderSideRunActions(item.draft)}
                            </div>
                          </div>
                          {item.run.kind === 'workflow' && item.steps.length > 0 && (
                            <div className="border-t px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', color: 'var(--fg-tertiary)' }}>
                              {item.steps.length} 个子步骤
                            </div>
                          )}
                        </div>
                      ))}
                    </section>
                  ))}
                </div>
              )}
            </>
          )}
        </aside>
      )}

      {/* Rename dialog */}
      <Dialog open={renameDialogOpen} onOpenChange={(open) => !open && handleRenameCancel()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重命名对话</DialogTitle>
          </DialogHeader>
          <Input
            value={renameTitle}
            onChange={(e) => setRenameTitle(e.target.value)}
            placeholder="请输入新标题"
            onKeyDown={(e) => e.key === 'Enter' && handleRenameConfirm()}
          />
          <DialogFooter>
            <Button variant="outline" onClick={handleRenameCancel}>取消</Button>
            <Button onClick={handleRenameConfirm}>确认</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Project folder dialog */}
      <Dialog open={projectFolderDialogMode !== null} onOpenChange={(open) => { if (!open) closeProjectFolderDialog(); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {projectFolderDialogMode === 'create' ? '新建文件夹' : '使用现有文件夹'}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <label className="text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
                文件夹路径
              </label>
              <Input
                value={projectFolderPath}
                onChange={(event) => setProjectFolderPath(event.target.value)}
                placeholder="D:\\Projects\\ChatTree"
                disabled={projectFolderSubmitting}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') handleProjectFolderSubmit();
                }}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium" style={{ color: 'var(--fg-tertiary)' }}>
                项目名称
              </label>
              <Input
                value={projectFolderLabel}
                onChange={(event) => setProjectFolderLabel(event.target.value)}
                placeholder="留空则使用文件夹名"
                disabled={projectFolderSubmitting}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') handleProjectFolderSubmit();
                }}
              />
            </div>
            {projectFolderError && (
              <div className="rounded-md px-3 py-2 text-xs" style={{
                color: 'var(--destructive)',
                background: 'color-mix(in srgb, var(--destructive) 10%, transparent)',
                border: '0.5px solid color-mix(in srgb, var(--destructive) 28%, transparent)',
              }}>
                {projectFolderError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeProjectFolderDialog} disabled={projectFolderSubmitting}>取消</Button>
            <Button onClick={handleProjectFolderSubmit} disabled={projectFolderSubmitting}>
              {projectFolderSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
              确认
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Image preview dialog */}
      <Dialog open={!!previewImage} onOpenChange={(open) => { if (!open) setPreviewImage(null); }}>
        <DialogContent className="max-w-[92vw] max-h-[92vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="truncate">{previewImage?.name}</DialogTitle>
          </DialogHeader>
          <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto rounded-md"
               style={{ background: 'var(--bg-button-tertiary-hover)' }}>
            {previewImage && (
              <img
                src={previewImage.url}
                alt={previewImage.name}
                className="max-h-[78vh] max-w-full object-contain"
              />
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Switch } from '@/components/ui/switch'
import { TextTooltip } from '@/components/ui/text-tooltip'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ArrowRight, Bot, Share2, StickyNote, X, Settings, Square, Plus, FileText, Pencil, Trash2, Check, Loader2, Workflow } from 'lucide-react'
import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useModelStore } from '../store/modelStore'
import { usePromptStore } from '../store/promtStore'
import { useNavigationStore } from '../store/navigationStore'
import { useConversationStore } from '../store/conversationStore'
import { slashRegistry } from '../services/slashRegistry'
import type { ToolPermissionMode } from '../types/message'
import type { MultiAgentMode } from '../types/conversation'
import type { SlashCommandInfo } from '../types/slash'
import {
  getPendingToolPermissionMode,
  markToolPermissionModeSent,
  selectToolPermissionMode,
  type ToolPermissionDraft,
} from '../utils/toolPermissionDraft'
import {
  applyReferNodeCompletion,
  applySlashCommandCompletion,
  getReferNodeCompletionCandidates,
  getReferNodeCompletionState,
  getSlashCompletionCandidates,
} from '../utils/slashRuntime'

type SystemPromptMode = 'override' | 'append';
type SuggestionItem =
  | { kind: 'slash'; key: string; command: SlashCommandInfo }
  | { kind: 'node'; key: string; node: ReturnType<typeof getReferNodeCompletionCandidates>[number] };

const MULTI_AGENT_MODE_OPTIONS: Array<{ value: MultiAgentMode; label: string; title: string }> = [
  { value: 'explicit_request_only', label: '显式', title: '显式请求时启用 subagent/workflow 工具' },
  { value: 'proactive', label: '自动', title: '允许模型主动使用 subagent/workflow 工具' },
  { value: 'none', label: '关闭', title: '不向模型提供 subagent/workflow 工具' },
];

interface Props {
  onSend: (
    value: string,
    modelId?: string,
    providerId?: string,
    toolPermissionMode?: ToolPermissionMode,
    promptId?: string | null,
    promptMode?: SystemPromptMode,
    multiAgentMode?: MultiAgentMode,
  ) => Promise<void>;
  onStop?: () => void;
  isStreaming?: boolean;
  disabled: boolean;
  conversationId: string | null;
  editValue?: string | null;
  isEditing?: boolean;
  onEditValueConsumed?: () => void;
  onCancelEdit?: () => void;
  attachedFiles?: string[];
  attachedImages?: Array<{ filename: string; url: string | null }>;
  onFilesPicked?: (files: File[]) => void;
  onRemoveFile?: (filename: string) => void;
  onPreviewImage?: (filename: string) => void;
  queuedMessages?: Array<{ id: string; content: string }>;
  onUpdateQueuedMessage?: (id: string, content: string) => void;
  onDeleteQueuedMessage?: (id: string) => void;
  settingsSlot?: ReactNode;
  toolPermissionDraft: ToolPermissionDraft;
  getToolPermissionDraft: () => ToolPermissionDraft;
  onToolPermissionDraftChange: (draft: ToolPermissionDraft) => void;
  pendingMultiAgentMode?: MultiAgentMode;
  onPendingMultiAgentModeChange?: (mode: MultiAgentMode) => void;
  variant?: 'dock' | 'composer';
}

export function ChatInput({
  onSend,
  onStop,
  isStreaming,
  disabled,
  conversationId,
  editValue,
  isEditing = false,
  onEditValueConsumed,
  onCancelEdit,
  attachedFiles = [],
  attachedImages = [],
  onFilesPicked,
  onRemoveFile,
  onPreviewImage,
  queuedMessages = [],
  onUpdateQueuedMessage,
  onDeleteQueuedMessage,
  settingsSlot,
  toolPermissionDraft,
  getToolPermissionDraft,
  onToolPermissionDraftChange,
  pendingMultiAgentMode = 'explicit_request_only',
  onPendingMultiAgentModeChange,
  variant = 'dock',
}: Props) {
  const { openSettings } = useNavigationStore();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const suggestionItemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [value, setValue] = useState('');
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [selectedPromptId, setSelectedPromptId] = useState<string | null>(null);
  const [selectedPromptTitle, setSelectedPromptTitle] = useState<string | null>(null);
  const [selectedPromptMode, setSelectedPromptMode] = useState<SystemPromptMode>('override');
  const [editingQueuedMessageId, setEditingQueuedMessageId] = useState<string | null>(null);
  const [slashCommands, setSlashCommands] = useState<SlashCommandInfo[]>(() => slashRegistry.list());
  const [slashHighlightIndex, setSlashHighlightIndex] = useState(0);
  const [slashDismissedForValue, setSlashDismissedForValue] = useState<string | null>(null);

  const {
    providers,
    models,
    currentProvider,
    currentModel,
    currentReasoningEffort,
    currentThinkingEnabled,
    pendingProvider,
    pendingModel,
    pendingReasoningEffort,
    pendingThinkingEnabled,
    config,
    loadModels,
    loadConfig,
    loadMetadata,
    getMetadata,
    setPendingProvider,
    setPendingModel,
    setPendingReasoningEffort,
    setPendingThinkingEnabled,
    confirmModelSelection,
    cancelModelSelection,
  } = useModelStore();

  const { prompts, loadPrompts, loadPrompt } = usePromptStore();
  const currentConversation = useConversationStore((s) => s.currentConversation);
  const treeData = useConversationStore((s) => s.treeData);
  const loadTree = useConversationStore((s) => s.loadTree);
  const updateMultiAgentMode = useConversationStore((s) => s.updateMultiAgentMode);
  const updateConversationModel = useConversationStore((s) => s.updateConversationModel);
  const currentMultiAgentMode: MultiAgentMode = currentConversation?.multi_agent_mode ?? pendingMultiAgentMode;
  const currentMultiAgentModeOption = MULTI_AGENT_MODE_OPTIONS.find((option) => option.value === currentMultiAgentMode)
    ?? MULTI_AGENT_MODE_OPTIONS[0];

  // 初始加载（loadConfig/loadProviders 已在 App.tsx 中调用）
  useEffect(() => {
    loadPrompts();
  }, []);

  useEffect(() => {
    let cancelled = false;
    setSlashCommands(slashRegistry.list());
    slashRegistry.refresh()
      .then((commands) => {
        if (!cancelled) setSlashCommands(commands);
      })
      .catch((err) => {
        console.error('加载斜杠命令失败:', err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 窗口焦点同步配置
  useEffect(() => {
    const onFocus = () => loadConfig();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, []);

  // 已启用的提供商
  const enabledProviders = providers.filter((provider) => {
    if (!config?.provider) return false;
    return config.provider[provider]?.enabled;
  });

  // 对话框中的活跃提供商和模型（pending 优先，否则 current）
  const activeDialogProvider = pendingProvider || currentProvider;
  const activeDialogModel = pendingModel || currentModel;

  // 当对话框中提供商改变时加载模型
  useEffect(() => {
    if (activeDialogProvider && modelDialogOpen) {
      loadModels(activeDialogProvider);
    }
  }, [activeDialogProvider, modelDialogOpen]);

  // 外部编辑值
  useEffect(() => {
    if (editValue != null) {
      setValue(editValue);
      onEditValueConsumed?.();
      requestAnimationFrame(() => { textareaRef.current?.focus(); });
    }
  }, [editValue]);

  // 切换会话清空输入
  useEffect(() => setValue(''), [conversationId]);

  // 自动调整 textarea 高度
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [value]);

  useEffect(() => {
    setSlashHighlightIndex(0);
  }, [value, slashCommands]);

  const referNodeCompletionState = useMemo(() => getReferNodeCompletionState(value), [value]);

  useEffect(() => {
    if (!conversationId || !referNodeCompletionState.active) return;
    loadTree(conversationId).catch((err) => {
      console.error('加载节点建议失败:', err);
    });
  }, [conversationId, currentConversation?.current_node_id, referNodeCompletionState.active, loadTree]);

  const handleSend = async () => {
    if (!value.trim() || (disabled && !isStreaming)) return;
    const draftAtSend = getToolPermissionDraft();
    const pendingToolPermissionMode = getPendingToolPermissionMode(draftAtSend);
    setValue('');
    await onSend(
      value,
      currentModel || undefined,
      currentProvider || undefined,
      pendingToolPermissionMode,
      selectedPromptId,
      selectedPromptId ? selectedPromptMode : undefined,
      currentMultiAgentMode,
    );
    onToolPermissionDraftChange(markToolPermissionModeSent(getToolPermissionDraft(), pendingToolPermissionMode));
  };

  const handleCancelEdit = () => {
    setValue('');
    onCancelEdit?.();
  };

  const handleFilePick = () => { fileInputRef.current?.click(); };

  const handleMultiAgentModeChange = (mode: string) => {
    const nextMode = mode as MultiAgentMode;
    if (!currentConversation) {
      onPendingMultiAgentModeChange?.(nextMode);
      return;
    }
    void updateMultiAgentMode(currentConversation.id, nextMode);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) onFilesPicked?.(Array.from(files));
    e.target.value = '';
  };

  // 打开模型对话框：将当前值复制到 pending（含推理设置）
  const handleOpenModelDialog = () => {
    setPendingProvider(currentProvider || '');
    setPendingModel(currentModel || '');
    setPendingReasoningEffort(currentReasoningEffort);
    setPendingThinkingEnabled(currentThinkingEnabled);
    if (currentProvider) loadMetadata(currentProvider);
    setModelDialogOpen(true);
  };

  // 按所选模型的元数据推导默认推理设置
  const applyDefaultsForModel = (provider: string, model: string) => {
    const meta = getMetadata(provider, model);
    const effortDefault = meta?.reasoning_effort?.default ?? null;
    const thinkingDefault = meta?.thinking?.toggleable
      ? (meta.thinking.default_enabled ?? false)
      : null;
    setPendingReasoningEffort(effortDefault);
    setPendingThinkingEnabled(thinkingDefault);
  };

  // 对话框中切换提供商
  const handleDialogProviderChange = (provider: string) => {
    setPendingProvider(provider);
    setPendingModel(''); // 切换提供商时清空模型选择
    setPendingReasoningEffort(null);
    setPendingThinkingEnabled(null);
    loadMetadata(provider);
  };

  // 对话框中切换模型：按新模型重置 pending 推理选择
  const handleDialogModelChange = (model: string) => {
    setPendingModel(model);
    if (activeDialogProvider) applyDefaultsForModel(activeDialogProvider, model);
  };

  // 确认模型选择：保存到后端（含推理设置）
  const handleConfirmModel = async () => {
    const result = confirmModelSelection();
    if (result && currentConversation) {
      try {
        await updateConversationModel(
          currentConversation.id,
          result.model,
          result.provider,
          result.reasoningEffort,
          result.thinkingEnabled,
        );
      } catch (err) {
        console.error('保存模型设置失败:', err);
      }
    }
    setModelDialogOpen(false);
  };

  // 取消模型选择
  const handleCancelModel = () => {
    cancelModelSelection();
    setModelDialogOpen(false);
  };

  // 对话框中提供商变更时加载模型（首次打开也需要）
  useEffect(() => {
    if (modelDialogOpen && activeDialogProvider) {
      loadModels(activeDialogProvider);
    }
  }, [modelDialogOpen]);

  const handlePromptSelect = async (promptId: string, promptTitle: string) => {
    if (selectedPromptId === promptId) {
      setSelectedPromptId(null);
      setSelectedPromptTitle(null);
      setSelectedPromptMode('override');
    } else {
      setSelectedPromptId(promptId);
      setSelectedPromptTitle(promptTitle);
      setSelectedPromptMode('override');
      await loadPrompt(promptId);
    }
  };

  const clearSelectedPrompt = () => {
    setSelectedPromptId(null);
    setSelectedPromptTitle(null);
    setSelectedPromptMode('override');
  };

  const getProviderDisplayName = (provider: string): string => {
    return config?.provider?.[provider]?.name || provider;
  };

  const isCurrentProxy = currentProvider ? config?.provider?.[currentProvider]?.source === 'reverse_proxy' : false;
  const isDialogProxy = activeDialogProvider ? config?.provider?.[activeDialogProvider]?.source === 'reverse_proxy' : false;

  const hiddenModels = activeDialogProvider ? (config?.provider?.[activeDialogProvider]?.hidden_models || []) : [];
  const dialogModels = (activeDialogProvider ? models[activeDialogProvider] || [] : []).filter(m => !hiddenModels.includes(m));
  const inputDisabled = disabled && !isStreaming;
  const sendDisabled = !value.trim() || (disabled && !isStreaming);
  const showStreamingSend = !!isStreaming && !!value.trim();
  const referNodeCandidates = referNodeCompletionState.active
    ? getReferNodeCompletionCandidates(value, treeData)
    : [];
  const slashCandidates = referNodeCompletionState.active
    ? []
    : getSlashCompletionCandidates(value, slashCommands);
  const suggestionItems: SuggestionItem[] = [
    ...slashCandidates.map((command) => ({ kind: 'slash' as const, key: `slash:${command.name}`, command })),
    ...referNodeCandidates.map((node) => ({ kind: 'node' as const, key: `node:${node.id}`, node })),
  ];
  const suggestionOpen = suggestionItems.length > 0 && slashDismissedForValue !== value;
  const highlightedSuggestion = suggestionOpen
    ? suggestionItems[Math.min(slashHighlightIndex, suggestionItems.length - 1)]
    : null;
  const currentPermissionLabel = {
    auto_approve: '自动批准',
    modify_only: '仅修改',
    ask_always: '全部需批准',
    plan: '计划模式',
  }[toolPermissionDraft.mode];

  // 所选模型的元数据 → 决定是否渲染推理强度/思考开关控件
  const activeMeta = getMetadata(activeDialogProvider, activeDialogModel);
  const effortSpec = activeMeta?.reasoning_effort;
  const thinkingSpec = activeMeta?.thinking;

  useEffect(() => {
    if (!suggestionOpen) return;
    const index = Math.min(slashHighlightIndex, Math.max(0, suggestionItems.length - 1));
    suggestionItemRefs.current[index]?.scrollIntoView({ block: 'nearest' });
  }, [slashHighlightIndex, suggestionItems.length, suggestionOpen]);

  const completeSlashCommand = (command: SlashCommandInfo) => {
    const nextValue = applySlashCommandCompletion(value, command);
    setValue(nextValue);
    setSlashHighlightIndex(0);
    setSlashDismissedForValue(command.name === 'refer' ? null : nextValue);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      const length = nextValue.length;
      textareaRef.current?.setSelectionRange(length, length);
    });
  };

  const completeSuggestion = (item: SuggestionItem) => {
    if (item.kind === 'slash') {
      completeSlashCommand(item.command);
      return;
    }
    const nextValue = applyReferNodeCompletion(value, item.node);
    setValue(nextValue);
    setSlashHighlightIndex(0);
    setSlashDismissedForValue(null);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      const length = nextValue.length;
      textareaRef.current?.setSelectionRange(length, length);
    });
  };

  return (
    <div className="relative w-full">
      {suggestionOpen && (
        <div
          className="absolute left-0 right-0 z-50 px-1"
          style={{ bottom: 'calc(100% + 8px)' }}
        >
          <div
            className="max-h-[320px] overflow-y-auto rounded-xl custom-scrollbar"
            style={{
              border: '0.5px solid var(--border)',
              background: 'color-mix(in srgb, var(--bg-input) 96%, var(--bg-button-tertiary-hover))',
              boxShadow: 'var(--shadow-xl), var(--highlight-top)',
              backdropFilter: 'blur(12px)',
            }}
          >
            {suggestionItems.map((item, index) => {
              const highlighted = index === Math.min(slashHighlightIndex, suggestionItems.length - 1);
              if (item.kind === 'node') {
                const node = item.node;
                return (
                  <button
                    ref={(element) => {
                      suggestionItemRefs.current[index] = element;
                    }}
                    key={item.key}
                    type="button"
                    className="flex min-h-[64px] w-full items-start gap-3 border-0 px-3 py-2 text-left text-xs transition-colors"
                    style={{
                      background: highlighted ? 'var(--accent-soft)' : 'transparent',
                      color: 'var(--fg-secondary)',
                    }}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      completeSuggestion(item);
                    }}
                    onMouseEnter={() => setSlashHighlightIndex(index)}
                  >
                    <span
                      className="mt-0.5 shrink-0 rounded-sm px-1.5 py-0.5 font-mono text-[10px]"
                      style={{
                        background: highlighted
                          ? 'color-mix(in srgb, var(--icon-accent) 16%, transparent)'
                          : 'var(--bg-button-tertiary-hover)',
                        color: highlighted ? 'var(--icon-accent)' : 'var(--fg-85)',
                      }}
                    >
                      node
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span
                          className="truncate font-mono text-[11px]"
                          style={{ color: highlighted ? 'var(--icon-accent)' : 'var(--fg-85)' }}
                        >
                          {node.insertText}
                        </span>
                        {node.isCurrent && (
                          <span className="shrink-0 rounded-full px-1.5 py-0.5 text-[10px]" style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
                            当前
                          </span>
                        )}
                        {!node.isCurrent && node.isOnCurrentBranch && (
                          <span className="shrink-0 rounded-full px-1.5 py-0.5 text-[10px]" style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-tertiary)' }}>
                            本分支
                          </span>
                        )}
                        {node.hasPruneSummary && (
                          <span className="shrink-0 rounded-full px-1.5 py-0.5 text-[10px]" style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-tertiary)' }}>
                            摘要
                          </span>
                        )}
                      </span>
                      <span className="mt-1 block truncate" style={{ color: 'var(--fg-secondary)' }}>
                        {node.userPreview || '无用户消息预览'}
                      </span>
                      <span className="mt-0.5 block truncate text-[11px]" style={{ color: 'var(--fg-tertiary)' }}>
                        {node.assistantPreview || node.modelId || '无助手回复预览'}
                      </span>
                    </span>
                  </button>
                );
              }
              const command = item.command;
              return (
                <button
                  ref={(element) => {
                    suggestionItemRefs.current[index] = element;
                  }}
                  key={item.key}
                  type="button"
                  className="flex h-11 w-full items-center gap-3 border-0 px-3 text-left text-xs transition-colors"
                  style={{
                    background: highlighted ? 'var(--accent-soft)' : 'transparent',
                    color: 'var(--fg-secondary)',
                  }}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    completeSuggestion(item);
                  }}
                  onMouseEnter={() => setSlashHighlightIndex(index)}
                >
                  <span
                    className="shrink-0 font-medium"
                    style={{ color: highlighted ? 'var(--icon-accent)' : 'var(--fg-85)' }}
                  >
                    /{command.name}
                  </span>
                  <span className="min-w-0 flex-1 truncate" style={{ color: 'var(--fg-tertiary)' }}>
                    {command.description}
                  </span>
                  {!command.blocks_main_thread && (
                    <span
                      className="shrink-0 rounded-full px-2 py-0.5"
                      style={{
                        background: 'color-mix(in srgb, var(--icon-accent) 14%, transparent)',
                        color: 'var(--icon-accent)',
                      }}
                    >
                      后台
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <div
        className={cn(
          'flex flex-col overflow-hidden transition-all',
          variant === 'composer' && 'new-chat-composer-shell',
        )}
        style={{
          border: '0.5px solid var(--border)',
          background: 'color-mix(in srgb, var(--bg-input) 90%, transparent)',
          boxShadow: 'var(--shadow-xl), var(--highlight-top)',
          backdropFilter: 'blur(12px)',
          borderRadius: variant === 'composer' ? '20px' : '16px',
        }}
        onFocus={(e) => {
          const el = e.currentTarget;
          el.style.borderColor = 'var(--border-focus)';
          el.style.boxShadow = 'var(--shadow-xl), 0 0 0 3px var(--accent-soft)';
        }}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node)) {
            const el = e.currentTarget;
            el.style.borderColor = 'var(--border)';
            el.style.boxShadow = 'var(--shadow-xl), var(--highlight-top)';
          }
        }}
      >
        {queuedMessages.length > 0 && (
          <div className="flex flex-col gap-1.5 px-3 pt-2 pb-1">
            {queuedMessages.map((message, index) => {
              const isEditing = editingQueuedMessageId === message.id;
              return (
                <div
                  key={message.id}
                  className="flex items-start gap-2 rounded-xl px-2.5 py-2 text-xs"
                  style={{
                    background: 'color-mix(in srgb, var(--accent-soft) 54%, transparent)',
                    border: '0.5px solid color-mix(in srgb, var(--icon-accent) 28%, transparent)',
                    color: 'var(--fg-secondary)',
                  }}
                >
                  <span className="shrink-0 pt-0.5 font-medium" style={{ color: 'var(--icon-accent)' }}>
                    排队 {index + 1}
                  </span>
                  {isEditing ? (
                    <textarea
                      className="min-h-7 flex-1 resize-none border-0 bg-transparent p-0 outline-none"
                      style={{ color: 'var(--fg-85)', fontFamily: 'var(--font-sans)' }}
                      value={message.content}
                      onChange={(e) => onUpdateQueuedMessage?.(message.id, e.target.value)}
                      autoFocus
                    />
                  ) : (
                    <span className="min-w-0 flex-1 whitespace-pre-wrap break-words leading-5">
                      {message.content}
                    </span>
                  )}
                  <TextTooltip content={isEditing ? '完成编辑' : '编辑'}>
                    <button
                      type="button"
                      className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 cursor-pointer"
                      style={{ color: 'var(--fg-tertiary)' }}
                      onClick={() => setEditingQueuedMessageId(isEditing ? null : message.id)}
                      aria-label={`${isEditing ? '完成编辑' : '编辑'}排队消息 ${index + 1}`}
                    >
                      {isEditing ? <Check className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
                    </button>
                  </TextTooltip>
                  <TextTooltip content="删除">
                    <button
                      type="button"
                      className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 cursor-pointer"
                      style={{ color: 'var(--fg-tertiary)' }}
                      onClick={() => {
                        if (editingQueuedMessageId === message.id) setEditingQueuedMessageId(null);
                        onDeleteQueuedMessage?.(message.id);
                      }}
                      aria-label={`删除排队消息 ${index + 1}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </TextTooltip>
                </div>
              );
            })}
          </div>
        )}
        {isEditing && (
          <div
            className="mx-3 mt-2 flex items-center justify-between gap-2 rounded-md px-2.5 py-1.5 text-xs"
            style={{
              background: 'var(--accent-soft)',
              color: 'var(--icon-accent)',
              border: '0.5px solid color-mix(in srgb, var(--icon-accent) 38%, transparent)',
            }}
          >
            <span className="min-w-0 truncate">编辑中</span>
            <TextTooltip content="取消编辑">
              <button
                type="button"
                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 cursor-pointer"
                style={{ color: 'var(--icon-accent)' }}
                onClick={handleCancelEdit}
                aria-label="取消编辑"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </TextTooltip>
          </div>
        )}
        {(attachedFiles.length > 0 || attachedImages.length > 0) && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2 pb-1">
            {attachedImages.map((image) => (
              <span
                key={image.filename}
                className="relative inline-flex h-14 w-14 overflow-hidden rounded-md"
                style={{ border: '0.5px solid var(--border)', background: 'var(--bg-button-tertiary-hover)' }}
              >
                <TextTooltip content={image.filename}>
                  <button
                    type="button"
                    className="h-full w-full cursor-zoom-in p-0 border-0 bg-transparent"
                    onClick={() => onPreviewImage?.(image.filename)}
                  >
                    {image.url ? (
                      <img
                        src={image.url}
                        alt={image.filename}
                        className="h-full w-full object-cover"
                      />
                    ) : (
                      <span className="flex h-full w-full items-center justify-center">
                        <Loader2 className="h-4 w-4 animate-spin" />
                      </span>
                    )}
                  </button>
                </TextTooltip>
                <button
                  type="button"
                  className="absolute right-0.5 top-0.5 flex h-4 w-4 items-center justify-center rounded-full border-0 p-0 cursor-pointer"
                  style={{ background: 'rgba(0,0,0,0.58)', color: 'white' }}
                  onClick={() => onRemoveFile?.(image.filename)}
                  aria-label={`移除 ${image.filename}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            {attachedFiles.map((fname) => (
              <span
                key={fname}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs"
                style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}
              >
                <FileText className="h-3 w-3" />
                <span className="max-w-[140px] truncate">{fname}</span>
                <button
                  className="ml-0.5 cursor-pointer bg-transparent border-none p-0"
                  style={{ color: 'var(--icon-accent)' }}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.color = 'var(--accent-red)'; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.color = 'var(--icon-accent)'; }}
                  onClick={() => onRemoveFile?.(fname)}
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className={cn(
            'w-full max-h-[200px] border-none outline-none resize-none leading-normal bg-transparent',
            variant === 'composer' ? 'min-h-[86px] px-5 pt-4 pb-2' : 'min-h-[60px] py-3 px-4',
          )}
          style={{
            fontSize: 'var(--codex-chat-font-size)',
            color: 'var(--fg-85)',
            fontFamily: 'var(--font-sans)',
          }}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            if (slashDismissedForValue && slashDismissedForValue !== e.target.value) {
              setSlashDismissedForValue(null);
            }
          }}
          onKeyDown={(e) => {
            if (suggestionOpen) {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSlashHighlightIndex((index) => (index + 1) % suggestionItems.length);
                return;
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSlashHighlightIndex((index) => (index - 1 + suggestionItems.length) % suggestionItems.length);
                return;
              }
              if ((e.key === 'Enter' || e.key === 'Tab') && highlightedSuggestion) {
                e.preventDefault();
                completeSuggestion(highlightedSuggestion);
                return;
              }
              if (e.key === 'Escape') {
                e.preventDefault();
                setSlashDismissedForValue(value);
                return;
              }
            }
            if (e.key === 'Enter' && !e.ctrlKey && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={inputDisabled}
          placeholder={variant === 'composer' ? '随心输入' : '按 Enter 发送，Ctrl+Enter 换行'}
          rows={2}
        />
        <div
          className={cn(
            'flex justify-between items-center px-2 py-1',
            variant === 'composer' && 'new-chat-composer-toolbar',
          )}
          style={{ borderTop: '0.5px solid var(--border)', background: 'var(--bg-button-tertiary-hover)' }}
        >
          <div className="flex gap-1 items-center min-w-0">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={handleFileChange}
            />
            <TextTooltip content="上传文件">
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                onClick={handleFilePick}
                aria-label="上传文件"
              >
                <Plus className="h-4 w-4" />
              </Button>
            </TextTooltip>
            {settingsSlot}
            {/* 模型选择按钮 */}
            <button
              className="flex items-center gap-1 text-xs font-normal h-7 px-2 rounded-full cursor-pointer transition-colors max-w-[210px]"
              style={{ color: 'var(--fg-tertiary)' }}
              onClick={handleOpenModelDialog}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; (e.currentTarget as HTMLElement).style.color = 'var(--fg-secondary)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; (e.currentTarget as HTMLElement).style.color = 'var(--fg-tertiary)'; }}
            >
              {isCurrentProxy ? <Share2 className="h-4 w-4 mr-1" style={{ color: 'var(--icon-accent)' }} /> : <Bot className="h-4 w-4 mr-1" />}
              {currentProvider && currentModel
                ? (
                  <span className="truncate flex items-center gap-1.5">
                    {isCurrentProxy ? (
                      <>
                        <span className="truncate" style={{ color: 'var(--icon-accent)' }}>{currentModel}</span>
                        <span className="text-[10px] px-1 py-0 rounded-full flex-shrink-0" style={{ background: 'rgba(99,179,237,0.15)', color: 'var(--icon-accent)' }}>
                          代理
                        </span>
                      </>
                    ) : (
                      <span className="truncate">{`${getProviderDisplayName(currentProvider)} / ${currentModel}`}</span>
                    )}
                  </span>
                )
                : '选择模型'}
            </button>

            {/* 提示词选择按钮 */}
            {selectedPromptTitle ? (
              <div className="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs max-w-[260px]"
                   style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
                <StickyNote className="h-3 w-3" />
                <TextTooltip content="更换提示词">
                  <button
                    className="truncate bg-transparent border-none p-0 text-xs cursor-pointer"
                    style={{ color: 'var(--icon-accent)' }}
                    onClick={() => setPromptDialogOpen(true)}
                  >
                    {selectedPromptTitle}
                  </button>
                </TextTooltip>
                <span
                  className="rounded-full px-1.5 py-0.5"
                  style={{ background: 'var(--bg-surface)', color: 'var(--fg-secondary)' }}
                >
                  {selectedPromptMode === 'override' ? '替换' : '追加'}
                </span>
                <button
                  className="ml-0.5 hover:opacity-70 cursor-pointer bg-transparent border-none p-0"
                  style={{ color: 'var(--icon-accent)' }}
                  onClick={clearSelectedPrompt}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ) : (
              <button
                className="flex items-center gap-1 text-xs font-normal h-7 px-2 rounded-full cursor-pointer transition-colors"
                style={{ color: 'var(--fg-tertiary)' }}
                onClick={() => setPromptDialogOpen(true)}
                onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; (e.currentTarget as HTMLElement).style.color = 'var(--fg-secondary)'; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; (e.currentTarget as HTMLElement).style.color = 'var(--fg-tertiary)'; }}
              >
                <StickyNote className="h-4 w-4 mr-1" />
                提示词
              </button>
            )}

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="flex items-center gap-1 text-xs font-normal h-7 px-2 rounded-full cursor-pointer transition-colors"
                  style={{ color: currentMultiAgentMode === 'none' ? 'var(--fg-tertiary)' : 'var(--icon-accent)' }}
                  aria-label={`Agent 模式：${currentMultiAgentModeOption.label}`}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                  }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
                >
                  <Workflow className="h-4 w-4 mr-1" />
                  Agent {currentMultiAgentModeOption.label}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-56">
                <DropdownMenuRadioGroup value={currentMultiAgentMode} onValueChange={handleMultiAgentModeChange}>
                  {MULTI_AGENT_MODE_OPTIONS.map((option) => (
                    <DropdownMenuRadioItem key={option.value} value={option.value} className="flex-col items-start gap-0.5">
                      <span>{option.label}</span>
                      <span className="text-[11px] leading-4" style={{ color: 'var(--fg-tertiary)' }}>{option.title}</span>
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          <div className="flex items-center gap-1">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="h-7 min-w-[66px] rounded-full px-2 text-xs font-medium flex items-center justify-center cursor-pointer transition-colors"
                  style={{
                    color: 'var(--fg-tertiary)',
                    border: '0.5px solid var(--border)',
                    background: 'color-mix(in srgb, var(--bg-input) 72%, transparent)',
                  }}
                  aria-label={`工具权限：${currentPermissionLabel}`}
                >
                  {currentPermissionLabel}
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                <DropdownMenuRadioGroup
                  value={toolPermissionDraft.mode}
                  onValueChange={(value) => {
                    onToolPermissionDraftChange(
                      selectToolPermissionMode(getToolPermissionDraft(), value as ToolPermissionMode),
                    );
                  }}
                >
                  <DropdownMenuRadioItem value="auto_approve">自动批准</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="modify_only">仅修改</DropdownMenuRadioItem>
                  <DropdownMenuRadioItem value="ask_always">全部需批准</DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            {/* 发送/终止按钮 */}
            {isStreaming && !showStreamingSend ? (
              <button
                className="w-7 h-7 rounded-full flex items-center justify-center cursor-pointer transition-all"
                style={{
                  background: 'var(--accent-red)',
                  color: '#fff',
                  border: 'none',
                }}
                onClick={onStop}
                aria-label="终止生成"
              >
                <Square className="h-3 w-3 fill-current" />
              </button>
            ) : (
              <button
                className="w-7 h-7 rounded-full flex items-center justify-center cursor-pointer transition-colors"
                style={{
                  background: sendDisabled
                    ? 'var(--bg-button-secondary)'
                    : 'var(--accent-active)',
                  color: sendDisabled ? 'var(--fg-tertiary)' : '#fff',
                  border: '0.5px solid var(--border)',
                  opacity: sendDisabled ? 0.35 : 1,
                }}
                onClick={handleSend}
                disabled={sendDisabled}
                aria-label={isStreaming ? '加入发送队列' : '发送消息'}
                onMouseEnter={(e) => {
                  if (!sendDisabled) {
                    (e.currentTarget as HTMLElement).style.background = 'var(--accent-hover)';
                  }
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.background = sendDisabled ? 'var(--bg-button-secondary)' : 'var(--accent-active)';
                }}
              >
                <ArrowRight className="h-4 w-4" style={{ transform: 'scaleX(1.14)' }} />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* 模型选择对话框 */}
      <Dialog open={modelDialogOpen} onOpenChange={(open) => { if (!open) handleCancelModel(); }}>
        <DialogContent className="flex flex-col" style={{ width: '480px', height: '420px' }} showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>选择模型</DialogTitle>
          </DialogHeader>
          <div className="flex gap-6 flex-1 min-h-0">
            {/* 提供商选择 - 左侧 */}
            <div className="flex flex-col gap-2 min-w-[120px] flex-1 overflow-y-auto pr-3 custom-scrollbar min-h-0" style={{ borderRight: '0.5px solid var(--border)' }}>
              <span className="font-semibold text-sm" style={{ color: 'var(--fg-tertiary)' }}>提供商</span>
              <RadioGroup
                value={activeDialogProvider || ''}
                onValueChange={(v) => handleDialogProviderChange(v)}
              >
                {enabledProviders.map((provider) => {
                  const isReverseProxy = config?.provider?.[provider]?.source === 'reverse_proxy';
                  return (
                    <div key={provider} className="flex items-center space-x-2">
                      <RadioGroupItem value={provider} id={`provider-${provider}`} />
                      <Label htmlFor={`provider-${provider}`} className="cursor-pointer flex items-center gap-1.5">
                        <span>{getProviderDisplayName(provider)}</span>
                        {isReverseProxy && (
                          <span className="text-[10px] px-1 py-0 rounded-full" style={{ background: 'rgba(99,179,237,0.15)', color: 'var(--icon-accent)' }}>
                            代理
                          </span>
                        )}
                      </Label>
                    </div>
                  );
                })}
              </RadioGroup>
              {enabledProviders.length === 0 && (
                <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  暂无已启用的提供商
                </span>
              )}
            </div>

            {/* 模型选择 - 右侧 */}
            <div className="flex flex-col gap-2 flex-1 overflow-y-auto custom-scrollbar min-h-0">
              <span className="font-semibold text-sm" style={{ color: 'var(--fg-tertiary)' }}>模型</span>
              <RadioGroup
                value={activeDialogModel || ''}
                onValueChange={handleDialogModelChange}
              >
                {dialogModels.map((model) => (
                  <div key={model} className="flex items-center space-x-2">
                    <RadioGroupItem value={model} id={`model-${model}`} />
                    <Label htmlFor={`model-${model}`} className="cursor-pointer flex items-center gap-1.5" style={isDialogProxy ? { color: 'var(--icon-accent)' } : undefined}>
                      {isDialogProxy && <Share2 className="h-3 w-3 flex-shrink-0" style={{ color: 'var(--icon-accent)' }} />}
                      {model}
                    </Label>
                  </div>
                ))}
              </RadioGroup>
              {dialogModels.length === 0 && (
                <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                  请先选择提供商
                </span>
              )}
            </div>
          </div>

          {/* 推理设置：仅当所选模型元数据声明支持时渲染 */}
          {(effortSpec || thinkingSpec?.toggleable) && (
            <div className="flex flex-col gap-3 pt-3" style={{ borderTop: '0.5px solid var(--border)' }}>
              {thinkingSpec?.toggleable && (
                <div className="flex items-center justify-between">
                  <Label htmlFor="thinking-switch" className="text-sm cursor-pointer" style={{ color: 'var(--fg-secondary)' }}>
                    思考模式
                  </Label>
                  <Switch
                    id="thinking-switch"
                    checked={!!pendingThinkingEnabled}
                    onCheckedChange={(checked) => setPendingThinkingEnabled(checked)}
                  />
                </div>
              )}
              {effortSpec && effortSpec.levels?.length > 0 && (
                <div className="flex flex-col gap-2">
                  <span className="text-sm" style={{ color: 'var(--fg-secondary)' }}>推理强度</span>
                  <div className="flex flex-wrap gap-1.5">
                    {effortSpec.levels.map((level) => {
                      const active = pendingReasoningEffort === level;
                      return (
                        <button
                          key={level}
                          className="px-2.5 py-1 rounded-full text-xs cursor-pointer transition-colors"
                          style={{
                            background: active ? 'var(--accent-soft)' : 'transparent',
                            color: active ? 'var(--icon-accent)' : 'var(--fg-tertiary)',
                            border: `0.5px solid ${active ? 'var(--icon-accent)' : 'var(--border)'}`,
                          }}
                          onClick={() => setPendingReasoningEffort(active ? null : level)}
                        >
                          {level}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                handleCancelModel();
                openSettings('providers');
              }}
            >
              <Settings className="h-4 w-4 mr-1" />
              设置
            </Button>
            <Button variant="outline" onClick={handleCancelModel}>
              取消
            </Button>
            <Button onClick={handleConfirmModel} disabled={!activeDialogProvider || !activeDialogModel}>
              确定
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 提示词选择对话框 */}
      <Dialog open={promptDialogOpen} onOpenChange={setPromptDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>选择提示词</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-1 max-h-[300px] overflow-y-auto custom-scrollbar">
            {prompts.length > 0 ? (
              prompts.map((prompt) => (
                <div
                  key={prompt.id}
                  className={cn(
                    'flex justify-between items-center p-2 rounded-lg cursor-pointer transition-colors',
                  )}
                  style={{
                    ...(selectedPromptId === prompt.id
                      ? { background: 'var(--accent-soft)' }
                      : {}),
                  }}
                  onClick={() => handlePromptSelect(prompt.id, prompt.title)}
                  onMouseEnter={(e) => {
                    if (selectedPromptId !== prompt.id) {
                      (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                    }
                  }}
                  onMouseLeave={(e) => {
                    if (selectedPromptId !== prompt.id) {
                      (e.currentTarget as HTMLElement).style.background = '';
                    }
                  }}
                >
                  <span className="text-sm">{prompt.title}</span>
                  {selectedPromptId === prompt.id && (
                    <span className="text-xs" style={{ color: 'var(--icon-accent)' }}>已选择</span>
                  )}
                </div>
              ))
            ) : (
              <span className="text-xs text-muted-foreground">
                暂无提示词，请先在提示词页面添加
              </span>
            )}
          </div>
          {selectedPromptId && (
            <div className="flex items-center justify-between gap-3 pt-2">
              <span className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                应用方式
              </span>
              <div
                className="inline-flex rounded-full p-0.5"
                style={{ background: 'var(--bg-button-tertiary-hover)' }}
              >
                {([
                  ['override', '替换 core'],
                  ['append', '追加 core'],
                ] as const).map(([mode, label]) => {
                  const active = selectedPromptMode === mode;
                  return (
                    <button
                      key={mode}
                      type="button"
                      className="rounded-full px-2.5 py-1 text-xs transition-colors"
                      style={{
                        background: active ? 'var(--accent-soft)' : 'transparent',
                        color: active ? 'var(--icon-accent)' : 'var(--fg-secondary)',
                      }}
                      onClick={() => setSelectedPromptMode(mode)}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={clearSelectedPrompt}>
              清除选择
            </Button>
            <Button onClick={() => setPromptDialogOpen(false)}>
              确定
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

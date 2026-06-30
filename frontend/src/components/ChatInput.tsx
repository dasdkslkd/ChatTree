import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Switch } from '@/components/ui/switch'
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
import { SendHorizontal, Bot, StickyNote, X, Settings, Square, Plus, FileText, Pencil, Trash2, Check } from 'lucide-react'
import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useModelStore } from '../store/modelStore'
import { usePromptStore } from '../store/promtStore'
import { useNavigationStore } from '../store/navigationStore'
import { useConversationStore } from '../store/conversationStore'
import { conversationApi } from '../api/conversation'
import { slashRegistry } from '../services/slashRegistry'
import type { ToolPermissionMode } from '../types/message'
import type { SlashCommandInfo } from '../types/slash'
import {
  getPendingToolPermissionMode,
  markToolPermissionModeSent,
  selectToolPermissionMode,
  type ToolPermissionDraft,
} from '../utils/toolPermissionDraft'
import {
  applySlashCommandCompletion,
  getSlashCompletionCandidates,
} from '../utils/slashRuntime'

interface Props {
  onSend: (
    value: string,
    modelId?: string,
    providerId?: string,
    toolPermissionMode?: ToolPermissionMode,
    systemPrompt?: string,
  ) => Promise<void>;
  onStop?: () => void;
  isStreaming?: boolean;
  disabled: boolean;
  conversationId: string | null;
  editValue?: string | null;
  onEditValueConsumed?: () => void;
  attachedFiles?: string[];
  attachedImages?: Array<{ filename: string; url: string }>;
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
  variant?: 'dock' | 'composer';
}

export function ChatInput({
  onSend,
  onStop,
  isStreaming,
  disabled,
  conversationId,
  editValue,
  onEditValueConsumed,
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
  variant = 'dock',
}: Props) {
  const { openSettings } = useNavigationStore();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState('');
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [selectedPromptId, setSelectedPromptId] = useState<string | null>(null);
  const [selectedPromptTitle, setSelectedPromptTitle] = useState<string | null>(null);
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

  const { prompts, currentPrompt, loadPrompts, loadPrompt } = usePromptStore();
  const currentConversation = useConversationStore((s) => s.currentConversation);

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

  const handleSend = async () => {
    if (!value.trim() || (disabled && !isStreaming)) return;
    const systemPrompt = currentPrompt?.content;
    const draftAtSend = getToolPermissionDraft();
    const pendingToolPermissionMode = getPendingToolPermissionMode(draftAtSend);
    setValue('');
    await onSend(
      value,
      currentModel || undefined,
      currentProvider || undefined,
      pendingToolPermissionMode,
      systemPrompt,
    );
    onToolPermissionDraftChange(markToolPermissionModeSent(getToolPermissionDraft(), pendingToolPermissionMode));
  };

  const handleFilePick = () => { fileInputRef.current?.click(); };

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
        await conversationApi.updateModel(
          currentConversation.id,
          result.model,
          result.provider,
          result.reasoningEffort,
          result.thinkingEnabled,
        );
        // 更新本地 conversation 对象
        const { conversations } = useConversationStore.getState();
        const conv = conversations.find(c => c.id === currentConversation.id);
        if (conv) {
          conv.model_id = result.model;
          conv.provider_id = result.provider;
          conv.reasoning_effort = result.reasoningEffort;
          conv.thinking_enabled = result.thinkingEnabled;
        }
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
    } else {
      setSelectedPromptId(promptId);
      setSelectedPromptTitle(promptTitle);
      await loadPrompt(promptId);
    }
  };

  const clearSelectedPrompt = () => {
    setSelectedPromptId(null);
    setSelectedPromptTitle(null);
  };

  const getProviderDisplayName = (provider: string): string => {
    return config?.provider?.[provider]?.name || provider;
  };

  const hiddenModels = activeDialogProvider ? (config?.provider?.[activeDialogProvider]?.hidden_models || []) : [];
  const dialogModels = (activeDialogProvider ? models[activeDialogProvider] || [] : []).filter(m => !hiddenModels.includes(m));
  const inputDisabled = disabled && !isStreaming;
  const sendDisabled = !value.trim() || (disabled && !isStreaming);
  const showStreamingSend = !!isStreaming && !!value.trim();
  const slashCandidates = getSlashCompletionCandidates(value, slashCommands);
  const slashCompletionOpen = slashCandidates.length > 0 && slashDismissedForValue !== value;
  const highlightedSlashCommand = slashCompletionOpen
    ? slashCandidates[Math.min(slashHighlightIndex, slashCandidates.length - 1)]
    : null;
  const currentPermissionLabel = {
    auto_approve: '自动批准',
    modify_only: '仅修改',
    ask_always: '全部需批准',
  }[toolPermissionDraft.mode];

  // 所选模型的元数据 → 决定是否渲染推理强度/思考开关控件
  const activeMeta = getMetadata(activeDialogProvider, activeDialogModel);
  const effortSpec = activeMeta?.reasoning_effort;
  const thinkingSpec = activeMeta?.thinking;

  const completeSlashCommand = (command: SlashCommandInfo) => {
    const nextValue = applySlashCommandCompletion(value, command);
    setValue(nextValue);
    setSlashHighlightIndex(0);
    setSlashDismissedForValue(nextValue);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      const length = nextValue.length;
      textareaRef.current?.setSelectionRange(length, length);
    });
  };

  return (
    <div className="relative w-full">
      {slashCompletionOpen && (
        <div
          className="absolute left-0 right-0 z-50 px-1"
          style={{ bottom: 'calc(100% + 8px)' }}
        >
          <div
            className="overflow-hidden rounded-xl"
            style={{
              border: '0.5px solid var(--border)',
              background: 'color-mix(in srgb, var(--bg-input) 96%, var(--bg-button-tertiary-hover))',
              boxShadow: 'var(--shadow-xl), var(--highlight-top)',
              backdropFilter: 'blur(12px)',
            }}
          >
            {slashCandidates.map((command, index) => {
              const highlighted = index === Math.min(slashHighlightIndex, slashCandidates.length - 1);
              return (
                <button
                  key={command.name}
                  type="button"
                  className="flex h-11 w-full items-center gap-3 border-0 px-3 text-left text-xs transition-colors"
                  style={{
                    background: highlighted ? 'var(--accent-soft)' : 'transparent',
                    color: 'var(--fg-secondary)',
                  }}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    completeSlashCommand(command);
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
                  <button
                    type="button"
                    className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 cursor-pointer"
                    style={{ color: 'var(--fg-tertiary)' }}
                    onClick={() => setEditingQueuedMessageId(isEditing ? null : message.id)}
                    aria-label={`${isEditing ? '完成编辑' : '编辑'}排队消息 ${index + 1}`}
                    title={isEditing ? '完成编辑' : '编辑'}
                  >
                    {isEditing ? <Check className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
                  </button>
                  <button
                    type="button"
                    className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 cursor-pointer"
                    style={{ color: 'var(--fg-tertiary)' }}
                    onClick={() => {
                      if (editingQueuedMessageId === message.id) setEditingQueuedMessageId(null);
                      onDeleteQueuedMessage?.(message.id);
                    }}
                    aria-label={`删除排队消息 ${index + 1}`}
                    title="删除"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              );
            })}
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
                <button
                  type="button"
                  className="h-full w-full cursor-zoom-in p-0 border-0 bg-transparent"
                  onClick={() => onPreviewImage?.(image.filename)}
                  title={image.filename}
                >
                  <img
                    src={image.url}
                    alt={image.filename}
                    className="h-full w-full object-cover"
                  />
                </button>
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
            if (slashCompletionOpen) {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSlashHighlightIndex((index) => (index + 1) % slashCandidates.length);
                return;
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSlashHighlightIndex((index) => (index - 1 + slashCandidates.length) % slashCandidates.length);
                return;
              }
              if ((e.key === 'Enter' || e.key === 'Tab') && highlightedSlashCommand) {
                e.preventDefault();
                completeSlashCommand(highlightedSlashCommand);
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
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0"
              onClick={handleFilePick}
              title="上传文件"
            >
              <Plus className="h-4 w-4" />
            </Button>
            {settingsSlot}
            {/* 模型选择按钮 */}
            <button
              className="flex items-center gap-1 text-xs font-normal h-7 px-2 rounded-full cursor-pointer transition-colors max-w-[210px]"
              style={{ color: 'var(--fg-tertiary)' }}
              onClick={handleOpenModelDialog}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; (e.currentTarget as HTMLElement).style.color = 'var(--fg-secondary)'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; (e.currentTarget as HTMLElement).style.color = 'var(--fg-tertiary)'; }}
            >
              <Bot className="h-4 w-4 mr-1" />
              {currentProvider && currentModel
                ? <span className="truncate">{`${getProviderDisplayName(currentProvider)} / ${currentModel}`}</span>
                : '选择模型'}
            </button>

            {/* 提示词选择按钮 */}
            {selectedPromptTitle ? (
              <div className="flex items-center gap-1 px-2 py-0.5 rounded-full text-xs"
                   style={{ background: 'var(--accent-soft)', color: 'var(--icon-accent)' }}>
                <StickyNote className="h-3 w-3" />
                <span>{selectedPromptTitle}</span>
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
                  title={`工具权限：${currentPermissionLabel}`}
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
                className="w-7 h-7 rounded-full flex items-center justify-center cursor-pointer transition-all"
                style={{
                  background: sendDisabled
                    ? 'var(--muted)'
                    : 'linear-gradient(160deg, var(--accent-hover), var(--accent-active))',
                  color: sendDisabled ? 'var(--fg-tertiary)' : '#fff5ef',
                  border: 'none',
                  boxShadow: sendDisabled ? 'none' : 'var(--glow-accent), inset 0 1px 0 rgba(255,255,255,0.25)',
                  opacity: sendDisabled ? 0.35 : 1,
                }}
                onClick={handleSend}
                disabled={sendDisabled}
                aria-label={isStreaming ? '加入发送队列' : '发送消息'}
                onMouseEnter={(e) => {
                  if (!sendDisabled) {
                    (e.currentTarget as HTMLElement).style.filter = 'brightness(1.08)';
                    (e.currentTarget as HTMLElement).style.transform = 'scale(1.06)';
                  }
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.filter = '';
                  (e.currentTarget as HTMLElement).style.transform = '';
                }}
              >
                <SendHorizontal className="h-4 w-4" />
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
                {enabledProviders.map((provider) => (
                  <div key={provider} className="flex items-center space-x-2">
                    <RadioGroupItem value={provider} id={`provider-${provider}`} />
                    <Label htmlFor={`provider-${provider}`} className="cursor-pointer">
                      {getProviderDisplayName(provider)}
                    </Label>
                  </div>
                ))}
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
                    <Label htmlFor={`model-${model}`} className="cursor-pointer">
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

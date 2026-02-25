import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { SendHorizontal, Bot, StickyNote, X, Settings, Square } from 'lucide-react'
import { useState, useEffect, useRef } from 'react'
import { useModelStore } from '../store/modelStore'
import { usePromptStore } from '../store/promtStore'
import { useNavigationStore } from '../store/navigationStore'
import type { ModelProvider } from '../types/model'

interface Props {
  onSend: (value: string, modelId?: string, systemPrompt?: string) => Promise<void>;
  onStop?: () => void;
  isStreaming?: boolean;
  disabled: boolean;
  conversationId: string | null;
}

export function ChatInput({ onSend, onStop, isStreaming, disabled, conversationId }: Props) {
  const { setCurrentPage } = useNavigationStore();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [value, setValue] = useState('');
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  const [promptDialogOpen, setPromptDialogOpen] = useState(false);
  const [selectedPromptId, setSelectedPromptId] = useState<string | null>(null);
  const [selectedPromptTitle, setSelectedPromptTitle] = useState<string | null>(null);

  const {
    providers,
    models,
    currentProvider,
    currentModel,
    config,
    loadProviders,
    loadModels,
    loadConfig,
    setCurrentProvider,
    setCurrentModel,
  } = useModelStore();

  const { prompts, currentPrompt, loadPrompts, loadPrompt } = usePromptStore();

  // 加载提供商、模型列表和配置
  useEffect(() => {
    loadProviders();
    loadConfig();
    loadPrompts();
  }, []);

  // 过滤已启用的提供商
  const enabledProviders = providers.filter((provider) => {
    if (!config?.provider) return false;
    return config.provider[provider]?.enabled;
  });

  // 当提供商改变时加载模型
  useEffect(() => {
    if (currentProvider) {
      loadModels(currentProvider);
    }
  }, [currentProvider]);

  // 切换会话清空输入
  useEffect(() => setValue(''), [conversationId]);

  // 自动调整 textarea 高度
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  }, [value]);

  const handleSend = async () => {
    if (!value.trim() || disabled) return;
    const systemPrompt = currentPrompt?.content;
    await onSend(value, currentModel || undefined, systemPrompt);
    setValue('');
  };

  const handleProviderChange = (provider: ModelProvider) => {
    setCurrentProvider(provider);
  };

  const handleModelChange = (model: string) => {
    setCurrentModel(model);
  };

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

  const getProviderDisplayName = (provider: ModelProvider): string => {
    const names: Record<ModelProvider, string> = {
      openai: 'OpenAI',
      azure: 'Azure',
      gemini: 'Gemini',
      ollama: 'Ollama',
      deepseek: 'DeepSeek',
      anthropic: 'Anthropic',
      groq: 'Groq',
      local: 'Local',
      nvidia: 'NVIDIA',
    };
    return names[provider] || provider;
  };

  const currentModels = currentProvider ? models[currentProvider] || [] : [];

  return (
    <div className="w-full">
      <div className="flex flex-col border rounded-xl bg-background overflow-hidden shadow-md">
        <textarea
          ref={textareaRef}
          className="w-full min-h-[60px] max-h-[200px] py-3 px-4 border-none outline-none resize-none text-sm leading-normal bg-transparent placeholder:text-muted-foreground disabled:bg-muted/50"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.ctrlKey && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={disabled}
          placeholder="按 Enter 发送，Ctrl+Enter 换行"
          rows={2}
        />
        <div className="flex justify-between items-center px-2 py-1 border-t bg-muted/30">
          <div className="flex gap-1 items-center">
            {/* 模型选择按钮 */}
            <Button
              variant="ghost"
              size="sm"
              className="text-xs font-normal h-7 px-2"
              onClick={() => setModelDialogOpen(true)}
            >
              <Bot className="h-4 w-4 mr-1" />
              {currentProvider && currentModel
                ? `${getProviderDisplayName(currentProvider)} / ${currentModel}`
                : '选择模型'}
            </Button>

            {/* 提示词选择按钮 */}
            {selectedPromptTitle ? (
              <div className="flex items-center gap-1 px-2 py-0.5 bg-primary/10 rounded-md text-xs">
                <StickyNote className="h-3 w-3" />
                <span>{selectedPromptTitle}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-4 w-4 p-0 hover:bg-transparent"
                  onClick={clearSelectedPrompt}
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                className="text-xs font-normal h-7 px-2"
                onClick={() => setPromptDialogOpen(true)}
              >
                <StickyNote className="h-4 w-4 mr-1" />
                提示词
              </Button>
            )}
          </div>

          {/* 发送/终止按钮 */}
          {isStreaming ? (
            <Button
              size="sm"
              variant="destructive"
              className="h-7 w-7 p-0"
              onClick={onStop}
              aria-label="终止生成"
            >
              <Square className="h-3 w-3 fill-current" />
            </Button>
          ) : (
            <Button
              size="sm"
              className="h-7 w-7 p-0"
              onClick={handleSend}
              disabled={disabled || !value.trim()}
              aria-label="发送消息"
            >
              <SendHorizontal className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* 模型选择对话框 */}
      <Dialog open={modelDialogOpen} onOpenChange={setModelDialogOpen}>
        <DialogContent className="max-w-fit">
          <DialogHeader>
            <DialogTitle>选择模型</DialogTitle>
          </DialogHeader>
          <div className="flex gap-6 min-w-[400px]">
            {/* 提供商选择 - 左侧 */}
            <div className="flex flex-col gap-2 min-w-[120px] max-h-[300px] overflow-y-auto pr-3 border-r">
              <span className="font-semibold text-sm text-muted-foreground">提供商</span>
              <RadioGroup
                value={currentProvider || ''}
                onValueChange={(v) => handleProviderChange(v as ModelProvider)}
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
                <span className="text-xs text-muted-foreground">
                  暂无已启用的提供商
                </span>
              )}
            </div>

            {/* 模型选择 - 右侧 */}
            <div className="flex flex-col gap-2 flex-1 max-h-[300px] overflow-y-auto">
              <span className="font-semibold text-sm text-muted-foreground">模型</span>
              <RadioGroup
                value={currentModel || ''}
                onValueChange={handleModelChange}
              >
                {currentModels.map((model) => (
                  <div key={model} className="flex items-center space-x-2">
                    <RadioGroupItem value={model} id={`model-${model}`} />
                    <Label htmlFor={`model-${model}`} className="cursor-pointer">
                      {model}
                    </Label>
                  </div>
                ))}
              </RadioGroup>
              {currentModels.length === 0 && (
                <span className="text-xs text-muted-foreground">
                  请先选择提供商
                </span>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setModelDialogOpen(false);
                setCurrentPage('settings');
              }}
            >
              <Settings className="h-4 w-4 mr-1" />
              设置
            </Button>
            <Button variant="outline" onClick={() => setModelDialogOpen(false)}>
              取消
            </Button>
            <Button onClick={() => setModelDialogOpen(false)}>
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
          <div className="flex flex-col gap-1 max-h-[300px] overflow-y-auto">
            {prompts.length > 0 ? (
              prompts.map((prompt) => (
                <div
                  key={prompt.id}
                  className={cn(
                    'flex justify-between items-center p-2 rounded-md cursor-pointer hover:bg-muted transition-colors',
                    selectedPromptId === prompt.id && 'bg-primary/10'
                  )}
                  onClick={() => handlePromptSelect(prompt.id, prompt.title)}
                >
                  <span className="text-sm">{prompt.title}</span>
                  {selectedPromptId === prompt.id && (
                    <span className="text-xs text-primary">已选择</span>
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

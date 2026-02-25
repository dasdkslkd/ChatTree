import { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Plus, Save, Pencil, Trash2, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { usePromptStore } from '../store/promtStore';
import type { Prompt, PromptResponse } from '../types/prompt';

export default function PromptsPage() {
  const { prompts, currentPrompt, loading, error, loadPrompts, loadPrompt, savePrompt, deletePrompt, clearCurrentPrompt } = usePromptStore();

  const [editingPrompt, setEditingPrompt] = useState<Prompt | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [promptToDelete, setPromptToDelete] = useState<PromptResponse | null>(null);

  useEffect(() => {
    loadPrompts();
  }, []);

  // 显示错误
  useEffect(() => {
    if (error) {
      toast.error(error);
    }
  }, [error]);

  // 选择提示词
  const handleSelectPrompt = async (prompt: PromptResponse) => {
    setIsNew(false);
    await loadPrompt(prompt.id);
  };

  // 当 currentPrompt 变化时更新编辑状态
  useEffect(() => {
    if (currentPrompt && !isNew) {
      setEditingPrompt({ ...currentPrompt });
    }
  }, [currentPrompt]);

  // 创建新提示词
  const handleCreateNew = () => {
    clearCurrentPrompt();
    const newPrompt: Prompt = {
      id: `prompt_${Date.now()}`,
      title: '新提示词',
      content: '',
    };
    setEditingPrompt(newPrompt);
    setIsNew(true);
  };

  // 保存提示词
  const handleSave = async () => {
    if (!editingPrompt) return;
    if (!editingPrompt.title.trim()) return;

    try {
      setSaving(true);
      await savePrompt(editingPrompt);
      toast.success('提示词保存成功！');
      setIsNew(false);
    } catch (err) {
      console.error(err);
    } finally {
      setSaving(false);
    }
  };

  // 删除提示词
  const handleDelete = async () => {
    if (!promptToDelete) return;
    try {
      await deletePrompt(promptToDelete.id);
      toast.success('提示词删除成功！');
      if (editingPrompt?.id === promptToDelete.id) {
        setEditingPrompt(null);
        setIsNew(false);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setDeleteDialogOpen(false);
      setPromptToDelete(null);
    }
  };

  // 取消编辑
  const handleCancel = () => {
    if (currentPrompt) {
      setEditingPrompt({ ...currentPrompt });
    } else {
      setEditingPrompt(null);
    }
    setIsNew(false);
  };

  return (
    <div className="flex flex-col h-screen bg-muted items-center">
      {/* 头部 */}
      <div className="px-6 py-5 border-b bg-background flex justify-between items-center w-full max-w-[900px]">
        <span className="text-lg font-semibold">系统提示词</span>
        <Button onClick={handleCreateNew}>
          <Plus className="h-4 w-4 mr-1" />
          新建提示词
        </Button>
      </div>

      {/* 内容区 */}
      <div className="flex-1 overflow-y-auto p-6 w-full max-w-[900px]">
        <div className="grid grid-cols-[280px_1fr] gap-6 h-full">
          {/* 左侧列表 */}
          <div className="flex flex-col gap-2">
            <span className="font-semibold mb-2 text-sm">提示词列表</span>
            <div className="flex flex-col gap-1 flex-1 overflow-y-auto p-2 border rounded-lg bg-background">
              {loading && prompts.length === 0 ? (
                <div className="flex justify-center items-center h-[200px]">
                  <Loader2 className="h-5 w-5 animate-spin" />
                </div>
              ) : prompts.length === 0 ? (
                <div className="flex flex-col items-center justify-center p-10 text-muted-foreground text-center">
                  <p className="text-xs">暂无提示词</p>
                  <p className="text-xs">点击"新建提示词"创建</p>
                </div>
              ) : (
                prompts.map((prompt) => (
                  <div
                    key={prompt.id}
                    className={cn(
                      'flex items-center justify-between px-3 py-2.5 rounded-md cursor-pointer transition-all hover:bg-muted',
                      currentPrompt?.id === prompt.id && 'bg-primary/10 hover:bg-primary/10'
                    )}
                    onClick={() => handleSelectPrompt(prompt)}
                  >
                    <span className="flex-1 truncate text-sm">{prompt.title}</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 w-7 p-0"
                      onClick={(e) => {
                        e.stopPropagation();
                        setPromptToDelete(prompt);
                        setDeleteDialogOpen(true);
                      }}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* 右侧编辑器 */}
          <div className="flex flex-col gap-4 p-4 border rounded-lg bg-background h-fit">
            {editingPrompt ? (
              <>
                <div className="space-y-2">
                  <Label>标题</Label>
                  <Input
                    value={editingPrompt.title}
                    onChange={(e) => setEditingPrompt({ ...editingPrompt, title: e.target.value })}
                    placeholder="输入提示词标题"
                  />
                </div>

                <div className="space-y-2">
                  <Label>内容</Label>
                  <Textarea
                    value={editingPrompt.content}
                    onChange={(e) => setEditingPrompt({ ...editingPrompt, content: e.target.value })}
                    placeholder="输入系统提示词内容..."
                    className="min-h-[300px] resize-y font-mono text-sm leading-relaxed"
                  />
                </div>

                <p className="text-xs text-muted-foreground">
                  提示: 系统提示词将作为对话的上下文，指导 AI 的行为和回复风格。
                </p>

                <Separator />

                <div className="flex gap-2 justify-end">
                  <Button variant="outline" onClick={handleCancel}>
                    取消
                  </Button>
                  <Button
                    onClick={handleSave}
                    disabled={saving || !editingPrompt.title.trim()}
                  >
                    {saving ? (
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4 mr-1" />
                    )}
                    {saving ? '保存中...' : '保存'}
                  </Button>
                </div>
              </>
            ) : (
              <div className="flex flex-col items-center justify-center p-10 text-muted-foreground text-center">
                <Pencil className="h-12 w-12 mb-4" />
                <p className="text-base">选择一个提示词进行编辑</p>
                <p className="text-xs">或点击"新建提示词"创建新的系统提示词</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 删除确认对话框 */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            确定要删除提示词 &ldquo;{promptToDelete?.title}&rdquo; 吗？此操作不可撤销。
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>取消</Button>
            <Button variant="destructive" onClick={handleDelete}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

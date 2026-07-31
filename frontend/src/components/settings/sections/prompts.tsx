import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Plus, Trash2, Loader2, Save, Pencil } from 'lucide-react';
import { toast } from 'sonner';
import { usePromptStore } from '@/store/promtStore';
import type { Prompt, PromptResponse } from '@/types/prompt';

export function PromptsSection() {
  const { prompts, currentPrompt, loading, loadPrompts, loadPrompt, savePrompt, deletePrompt, clearCurrentPrompt } = usePromptStore();

  const [editingPrompt, setEditingPrompt] = useState<Prompt | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [promptToDelete, setPromptToDelete] = useState<PromptResponse | null>(null);

  useEffect(() => { loadPrompts(); }, [loadPrompts]);

  const handleSelectPrompt = async (prompt: PromptResponse) => {
    setIsNew(false);
    await loadPrompt(prompt.id);
  };

  useEffect(() => {
    if (currentPrompt && !isNew) {
      setEditingPrompt({ ...currentPrompt });
    }
  }, [currentPrompt, isNew]);

  const handleCreateNew = () => {
    clearCurrentPrompt();
    setEditingPrompt({ id: `prompt_${Date.now()}`, title: '新提示词', content: '' });
    setIsNew(true);
  };

  const handleSave = async () => {
    if (!editingPrompt || !editingPrompt.title.trim()) return;
    try {
      setSaving(true);
      await savePrompt(editingPrompt);
      toast.success('保存成功');
      setIsNew(false);
    } catch {
      // error handled by store
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!promptToDelete) return;
    try {
      await deletePrompt(promptToDelete.id);
      toast.success('已删除');
      if (editingPrompt?.id === promptToDelete.id) {
        setEditingPrompt(null);
        setIsNew(false);
      }
    } catch {
      // error handled by store
    } finally {
      setDeleteDialogOpen(false);
      setPromptToDelete(null);
    }
  };

  const handleCancel = () => {
    if (currentPrompt) setEditingPrompt({ ...currentPrompt });
    else setEditingPrompt(null);
    setIsNew(false);
  };

  return (
    <div className="flex h-full">
      {/* Left: prompt list */}
      <div
        className="flex flex-col flex-shrink-0 overflow-hidden"
        style={{ width: '220px', borderRight: '0.5px solid var(--border)' }}
      >
        <div
          className="flex items-center justify-between px-3 py-2.5 flex-shrink-0"
          style={{ borderBottom: '0.5px solid var(--border)' }}
        >
          <span className="text-sm font-medium" style={{ color: 'var(--fg-85)' }}>提示词</span>
          <button
            className="w-6 h-6 flex items-center justify-center rounded cursor-pointer bg-transparent border-none"
            style={{ color: 'var(--icon-tertiary)' }}
            onClick={handleCreateNew}
            onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)'; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = ''; }}
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-1.5 custom-scrollbar">
          {loading && prompts.length === 0 ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="h-4 w-4 animate-spin" style={{ color: 'var(--icon-accent)' }} />
            </div>
          ) : prompts.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs" style={{ color: 'var(--fg-tertiary)' }}>
              暂无提示词
            </div>
          ) : (
            prompts.map((prompt) => (
              <div
                key={prompt.id}
                className="flex items-center justify-between px-2.5 py-1.5 rounded-lg cursor-pointer transition-colors group"
                style={{
                  background: currentPrompt?.id === prompt.id ? 'var(--bg-button-tertiary-active)' : undefined,
                  color: 'var(--fg-85)',
                }}
                onClick={() => handleSelectPrompt(prompt)}
                onMouseEnter={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = 'var(--bg-button-tertiary-hover)';
                }}
                onMouseLeave={(e) => {
                  if (currentPrompt?.id !== prompt.id) (e.currentTarget as HTMLElement).style.background = '';
                }}
              >
                <span className="flex-1 truncate text-sm">{prompt.title}</span>
                <button
                  className="w-5 h-5 flex items-center justify-center rounded cursor-pointer bg-transparent border-none opacity-0 group-hover:opacity-100 transition-opacity"
                  style={{ color: 'var(--icon-tertiary)' }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setPromptToDelete(prompt);
                    setDeleteDialogOpen(true);
                  }}
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Right: editor */}
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        {/* Fixed header */}
        <div className="flex-shrink-0 px-6 pt-6 pb-4">
          <h1 className="text-2xl font-semibold mb-1" style={{ color: 'var(--fg-85)' }}>
            {editingPrompt ? (isNew ? '新建提示词' : '编辑提示词') : '提示词'}
          </h1>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            {editingPrompt ? '配置系统提示词' : '管理系统提示词'}
          </p>
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto custom-scrollbar min-h-0 px-6 pb-6">
          {editingPrompt ? (
            <div className="space-y-4 max-w-[500px]">
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
                  className="min-h-[250px] resize-y text-sm leading-relaxed"
                />
              </div>
              <p className="text-xs" style={{ color: 'var(--fg-tertiary)' }}>
                系统提示词将作为对话的上下文，指导 AI 的行为和回复风格。
              </p>
              <div className="flex gap-2 justify-end pt-2">
                <Button variant="outline" onClick={handleCancel}>取消</Button>
                <Button onClick={handleSave} disabled={saving || !editingPrompt.title.trim()}>
                  {saving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
                  {saving ? '保存中...' : '保存'}
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full" style={{ color: 'var(--fg-tertiary)' }}>
              <Pencil className="h-10 w-10 mb-3" />
              <p className="text-sm">选择一个提示词进行编辑</p>
              <p className="text-xs mt-1">或点击 + 创建新的</p>
            </div>
          )}
        </div>

        {/* Footer spacer */}
        <div className="flex-shrink-0 h-2" />
      </div>

      {/* Delete confirm */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent className="max-w-[360px]">
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm" style={{ color: 'var(--fg-secondary)' }}>
            确定要删除 &ldquo;{promptToDelete?.title}&rdquo; 吗？
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

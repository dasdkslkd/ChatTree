import type { GenerationInfo } from '../types/message';

export function getGenerationStatusText(generationInfo?: Pick<GenerationInfo, 'status' | 'error_message'> | null): string | null {
  if (!generationInfo || generationInfo.status === 'completed') return null;
  const errorMessage = generationInfo.error_message?.trim();
  if (errorMessage) return errorMessage;
  return generationInfo.status === 'stopped' ? '已停止' : '生成出错';
}

export function getStreamStatusText(status: string, errorMessage?: string | null): string | null {
  if (status === 'error') return errorMessage?.trim() || '生成出错';
  if (status === 'stopped') return '已停止';
  return null;
}

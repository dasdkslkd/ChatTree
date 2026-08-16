import { toast as sonnerToast, type ExternalToast } from 'sonner';
import { recordError } from './errorHistory';

// 统一错误收集 + toast 生命周期入口：业务代码统一从 @/utils/toast 导入 toast。
// 每个 toast 注入唯一 id/testId（DOM 上的 data-testid 可映射回 toast id），
// 并关闭 sonner 自带计时器（duration: Infinity），改由 <Toaster> 的 CSS 动画
// animationend 驱动消失——保证「停留 1s + 渐变 1s」，hover 暂停、移开后重新完整计时。
const TOAST_LIFETIME = 2000;
let toastSeq = 0;

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

function withToastId<T extends ExternalToast | undefined>(data: T): T & { id: string; testId: string; duration: number } {
  const id = `app-toast-${++toastSeq}`;
  return {
    ...data,
    id,
    testId: id,
    // 减弱动效时 sonner 会禁用 CSS 动画，回退到自带计时器保证 toast 仍会消失
    duration: prefersReducedMotion() ? TOAST_LIFETIME : Infinity,
  };
}

const appToast = ((message, data) => sonnerToast(message, withToastId(data))) as typeof sonnerToast;
Object.assign(appToast, sonnerToast);

for (const type of ['error', 'success', 'info', 'warning', 'message', 'loading'] as const) {
  const original = sonnerToast[type];
  appToast[type] = ((message, data) => {
    if (type === 'error' && typeof message === 'string') recordError(message);
    return original(message, withToastId(data));
  }) as typeof original;
}

export const toast = appToast;

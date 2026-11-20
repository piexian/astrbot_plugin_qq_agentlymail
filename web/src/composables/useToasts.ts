/** toast 队列：3200ms 自动移除，作用域销毁时清理定时器。 */

import { onScopeDispose, shallowRef } from "vue";

export type Notify = (message: string) => void;

export interface Toast {
  id: number;
  message: string;
}

export function useToasts() {
  const toasts = shallowRef<Toast[]>([]);
  const timers = new Set<number>();
  let nextId = 0;

  function notify(message: string) {
    const id = ++nextId;
    toasts.value = [...toasts.value, { id, message }];
    const timer = window.setTimeout(() => {
      timers.delete(timer);
      toasts.value = toasts.value.filter((toast) => toast.id !== id);
    }, 3200);
    timers.add(timer);
  }

  onScopeDispose(() => {
    timers.forEach((timer) => window.clearTimeout(timer));
    timers.clear();
  });

  return { toasts, notify };
}

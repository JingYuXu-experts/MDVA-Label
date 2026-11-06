/** toast store：右下角堆叠，4s 自动消失，可手动关闭（UI规格 §4 Toast） */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastType = 'success' | 'danger' | 'warning' | 'info'

export interface ToastItem {
  id: number
  type: ToastType
  text: string
}

let seq = 0

export const useToastStore = defineStore('toast', () => {
  const toasts = ref<ToastItem[]>([])

  function push(type: ToastType, text: string, ms = 4000) {
    const id = ++seq
    toasts.value.push({ id, type, text })
    if (ms > 0) setTimeout(() => dismiss(id), ms)
    return id
  }

  function dismiss(id: number) {
    const idx = toasts.value.findIndex(t => t.id === id)
    if (idx >= 0) toasts.value.splice(idx, 1)
  }

  return { toasts, push, dismiss }
})

export function useToast() {
  const store = useToastStore()
  return {
    success: (text: string) => store.push('success', text),
    danger: (text: string) => store.push('danger', text),
    warning: (text: string) => store.push('warning', text),
    info: (text: string) => store.push('info', text),
  }
}

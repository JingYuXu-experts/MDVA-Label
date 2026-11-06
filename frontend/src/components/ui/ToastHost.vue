<script setup lang="ts">
import { X, CheckCircle2, AlertTriangle, XCircle, Info } from 'lucide-vue-next'
import { useToastStore, type ToastType } from '@/stores/toast'

const store = useToastStore()

const icons: Record<ToastType, unknown> = {
  success: CheckCircle2, danger: XCircle, warning: AlertTriangle, info: Info,
}
</script>

<template>
  <div class="toasts" aria-live="polite">
    <div v-for="toast in store.toasts" :key="toast.id" class="toast" :class="`toast--${toast.type}`"
      role="status">
      <component :is="icons[toast.type]" :size="14" class="toast__icon" aria-hidden="true" />
      <span class="toast__text">{{ toast.text }}</span>
      <button type="button" class="toast__close" aria-label="关闭提示" @click="store.dismiss(toast.id)">
        <X :size="12" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed; right: var(--sp-4); bottom: var(--sp-4); z-index: 200;
  display: flex; flex-direction: column; gap: var(--sp-2); max-width: 360px;
}
.toast {
  display: flex; align-items: flex-start; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-left: 3px solid var(--text-muted);
  border-radius: var(--radius-pop); box-shadow: var(--shadow-pop);
  font-size: var(--fs-ui);
}
.toast--success { border-left-color: var(--success); }
.toast--success .toast__icon { color: var(--success); }
.toast--danger { border-left-color: var(--danger); }
.toast--danger .toast__icon { color: var(--danger); }
.toast--warning { border-left-color: var(--warning); }
.toast--warning .toast__icon { color: var(--warning); }
.toast--info { border-left-color: var(--accent); }
.toast--info .toast__icon { color: var(--accent); }
.toast__icon { flex: none; margin-top: 1px; }
.toast__text { flex: 1; word-break: break-all; }
.toast__close {
  flex: none; display: flex; align-items: center;
  background: transparent; border: none; padding: 0;
  color: var(--text-muted); cursor: pointer;
}
.toast__close:hover { color: var(--text-primary); }
</style>

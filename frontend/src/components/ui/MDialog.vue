<script setup lang="ts">
import { nextTick, ref, watch, onBeforeUnmount } from 'vue'
import { X } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  open: boolean
  title: string
  width?: 480 | 640 | 800
}>(), { open: false, width: 480 })

const emit = defineEmits<{ close: [] }>()

const panelEl = ref<HTMLElement | null>(null)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.stopPropagation(); emit('close'); return }
  if (event.key !== 'Tab' || !panelEl.value) return
  // 焦点圈闭（UI规格 §9）
  const focusables = panelEl.value.querySelectorAll<HTMLElement>(
    'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
  if (!focusables.length) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}

watch(() => props.open, async (value) => {
  if (value) {
    document.addEventListener('keydown', onKeydown, true)
    await nextTick()
    panelEl.value?.querySelector<HTMLElement>('button, input, select, textarea, [tabindex]')?.focus()
  } else {
    document.removeEventListener('keydown', onKeydown, true)
  }
})

onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown, true))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog__mask" @click.self="emit('close')">
      <div class="dialog" :style="{ width: width + 'px' }" role="dialog" aria-modal="true"
        :aria-label="title" ref="panelEl">
        <header class="dialog__head">
          <h2 class="dialog__title">{{ title }}</h2>
          <button type="button" class="dialog__close" aria-label="关闭" @click="emit('close')">
            <X :size="14" />
          </button>
        </header>
        <div class="dialog__body"><slot /></div>
        <footer v-if="$slots.footer" class="dialog__foot"><slot name="footer" /></footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.dialog__mask {
  position: fixed; inset: 0; z-index: 100;
  background: rgba(0, 0, 0, 0.5);
  display: flex; align-items: center; justify-content: center;
}
.dialog {
  max-width: calc(100vw - 32px); max-height: calc(100vh - 64px);
  display: flex; flex-direction: column;
  background: var(--bg-panel); border: 1px solid var(--border-strong);
  border-radius: var(--radius-pop); box-shadow: var(--shadow-pop);
}
.dialog__head {
  display: flex; align-items: center; justify-content: space-between;
  padding: var(--sp-3) var(--sp-4); border-bottom: 1px solid var(--border); flex: none;
}
.dialog__title { margin: 0; font-size: var(--fs-md); font-weight: 600; }
.dialog__close {
  display: flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0;
  background: transparent; border: none; border-radius: var(--radius-control);
  color: var(--text-secondary); cursor: pointer;
}
.dialog__close:hover { background: var(--bg-hover); color: var(--text-primary); }
.dialog__body { padding: var(--sp-4); overflow-y: auto; }
.dialog__foot {
  display: flex; justify-content: flex-end; gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4); border-top: 1px solid var(--border); flex: none;
}
</style>

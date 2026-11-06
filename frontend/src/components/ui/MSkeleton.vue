<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'

/** >600ms 才出现，避免闪烁（UI规格 §4 Skeleton） */
const props = withDefaults(defineProps<{
  show: boolean
  variant?: 'rows' | 'grid'
  count?: number
}>(), { show: false, variant: 'rows', count: 6 })

const visible = ref(false)
let timer: ReturnType<typeof setTimeout> | null = null

watch(() => props.show, (value) => {
  if (timer) { clearTimeout(timer); timer = null }
  if (value) timer = setTimeout(() => { visible.value = true }, 600)
  else visible.value = false
}, { immediate: true })

onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
</script>

<template>
  <div v-if="show && visible" class="skeleton" :class="`skeleton--${variant}`" aria-hidden="true">
    <span v-for="i in count" :key="i" class="skeleton__cell" />
  </div>
</template>

<style scoped>
.skeleton { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-2); }
.skeleton--grid { display: grid; grid-template-columns: repeat(auto-fill, 160px); gap: var(--sp-2); }
.skeleton__cell {
  display: block; height: 32px; border-radius: var(--radius-control);
  background: linear-gradient(90deg, var(--bg-raised) 25%, var(--bg-hover) 50%, var(--bg-raised) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s infinite;
}
.skeleton--grid .skeleton__cell { height: 100px; }
@keyframes shimmer { to { background-position: -200% 0; } }
</style>

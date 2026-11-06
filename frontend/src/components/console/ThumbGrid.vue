<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch, nextTick } from 'vue'
import ThumbCell from './ThumbCell.vue'
import { useItemsStore } from '@/stores/items'
import type { ItemRow } from '@/api/types'

const props = defineProps<{
  items: ItemRow[]
  activeUri: string
}>()

const emit = defineEmits<{ 'pick': [uri: string] }>()

const store = useItemsStore()
const gridEl = ref<HTMLElement | null>(null)

let observer: IntersectionObserver | null = null
const queue: string[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null

/** 可视区批量：单批 ≤32（UI规格 §8）；200ms 汇聚一次，滚动中不逐格发请求 */
function scheduleFetch() {
  if (flushTimer) return
  flushTimer = setTimeout(() => {
    flushTimer = null
    const batch = queue.splice(0, 32)
    if (batch.length) void store.fetchThumbs(batch)
    if (queue.length) scheduleFetch()
  }, 200)
}

function observe(root: HTMLElement) {
  observer?.disconnect()
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue
      const uri = (entry.target as HTMLElement).dataset.uri
      if (uri && !store.thumbs.has(uri) && !queue.includes(uri)) {
        queue.push(uri)
        observer?.unobserve(entry.target)
      }
    }
    if (queue.length) scheduleFetch()
  }, { root, rootMargin: '200px' })
  for (const el of root.querySelectorAll<HTMLElement>('[data-uri]')) observer.observe(el)
}

async function reobserve() {
  await nextTick()
  if (gridEl.value) observe(gridEl.value)
}

watch(() => props.items, () => { void reobserve() })

onMounted(() => { void reobserve() })
onBeforeUnmount(() => {
  observer?.disconnect()
  if (flushTimer) clearTimeout(flushTimer)
})
</script>

<template>
  <div class="grid" ref="gridEl">
    <div v-for="item in items" :key="item.uri" :data-uri="item.uri">
      <ThumbCell :item="item"
        :thumb="store.thumbs.get(item.uri)"
        :loading="store.thumbsLoading.has(item.uri)"
        :selected="store.selected.has(item.uri)"
        :active="item.uri === activeUri"
        @click="emit('pick', item.uri)"
        @toggle-select="store.toggleSelect(item.uri)" />
    </div>
  </div>
</template>

<style scoped>
.grid {
  display: grid; grid-template-columns: repeat(auto-fill, 160px);
  gap: var(--sp-2); align-content: start;
  padding: var(--sp-2);
}
</style>

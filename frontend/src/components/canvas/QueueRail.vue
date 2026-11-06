<script setup lang="ts">
import { computed, ref } from 'vue'
import { useItemsStore, bucketOf } from '@/stores/items'
import { useCanvasStore } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'
import * as api from '@/api/endpoints'

const emit = defineEmits<{ pick: [uri: string] }>()

const items = useItemsStore()
const store = useCanvasStore()
const ctx = useCtxStore()

const reviewOnly = ref(false)
const hardUris = ref<Set<string>>(new Set())

const shown = computed(() => {
  const all = items.items
  if (reviewOnly.value) return all.filter((it) => (it.states?.submitted || 0) > 0)
  return all
})

const BUCKET_COLOR: Record<string, string> = {
  unlabeled: 'var(--text-muted)', draft: 'var(--warning)',
  submitted: 'var(--accent)', approved: 'var(--success)',
}

function dotColor(uri: string): string {
  const it = items.items.find((x) => x.uri === uri)
  if (!it) return 'var(--text-muted)'
  return BUCKET_COLOR[bucketOf(it)] || 'var(--text-muted)'
}

function shortName(name: string): string {
  const stem = name.replace(/\.[^.]+$/, '')
  return stem.slice(0, 2).toUpperCase()
}

async function loadHard(): Promise<void> {
  if (!ctx.version) return
  try {
    const resp = await api.smartHard(ctx.version, [], { k: 20 })
    const list = (resp.hard as { uri?: string }[]) || (resp.items as { uri?: string }[]) || []
    hardUris.value = new Set(list.map((x) => x.uri).filter(Boolean) as string[])
  } catch {
    hardUris.value = new Set()
  }
}
loadHard()
</script>

<template>
  <div class="rail" role="listbox" aria-label="条目队列">
    <button
      v-if="ctx.role !== 'annotator'"
      class="rail__filter"
      :class="{ 'is-on': reviewOnly }"
      :title="reviewOnly ? '显示全部条目' : '仅显示待审条目'"
      @click="reviewOnly = !reviewOnly"
    >审</button>
    <button
      v-for="it in shown"
      :key="it.uri"
      class="rail__dot"
      :class="{ 'is-cur': it.uri === store.currentUri }"
      role="option"
      :aria-selected="it.uri === store.currentUri"
      :title="it.uri"
      @click="emit('pick', it.uri)"
    >
      <span class="rail__glyph">{{ shortName(it.name) }}</span>
      <span class="rail__state" :style="{ background: dotColor(it.uri) }" />
      <span v-if="hardUris.has(it.uri)" class="rail__hard" aria-label="难例" />
    </button>
    <div v-if="!shown.length" class="rail__empty">空</div>
  </div>
</template>

<style scoped>
.rail { width: var(--rail-w); flex: none; background: var(--bg-panel); border-right: 1px solid var(--border); overflow-y: auto; display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 6px 0; }
.rail__filter { font-size: 10px; color: var(--text-secondary); background: var(--bg-raised); border: 1px solid var(--border); border-radius: 3px; width: 26px; height: 20px; cursor: pointer; }
.rail__filter.is-on { color: var(--accent); border-color: var(--accent); }
.rail__dot { position: relative; width: 26px; height: 26px; border-radius: 3px; border: 1px solid var(--border); background: var(--bg-raised); cursor: pointer; display: flex; align-items: center; justify-content: center; }
.rail__dot.is-cur { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.rail__glyph { font-size: 10px; color: var(--text-secondary); font-family: var(--font-mono); }
.rail__state { position: absolute; right: 2px; bottom: 2px; width: 6px; height: 6px; border-radius: 50%; }
.rail__hard { position: absolute; left: 2px; top: 2px; width: 6px; height: 6px; border-radius: 50%; background: var(--danger); }
.rail__empty { font-size: 10px; color: var(--text-muted); }
</style>

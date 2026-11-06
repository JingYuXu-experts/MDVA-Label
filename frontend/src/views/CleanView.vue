<script setup lang="ts">
/**
 * V3 清洗 `/clean`（UI规格 §5.3）：左 280 算子面板 / 中央预览 / 右 320 执行与回退。
 * 作用范围 = 当前数据集+版本的条目全集（与 /api/items 同源，不另设筛选）。
 */
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import OpPanel from '@/components/clean/OpPanel.vue'
import PreviewPane from '@/components/clean/PreviewPane.vue'
import ExecPanel from '@/components/clean/ExecPanel.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import { useCleanStore } from '@/stores/clean'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore } from '@/stores/items'

const store = useCleanStore()
const ctx = useCtxStore()
const items = useItemsStore()

const scopeUris = computed(() => items.items.map(i => i.uri))
const currentVersion = computed(() => ctx.versions.find(v => v.id === ctx.version) || null)
/** 只有清洗产出的版本才谈得上"丢弃明细/回退"；判据来自版本 op，不由前端猜 */
const isCleanVersion = computed(() => String(currentVersion.value?.op || '').startsWith('clean'))

onMounted(async () => {
  store.datasetId = ctx.dataset
  await Promise.all([store.loadOps(), store.loadHistory()])
})

onBeforeUnmount(() => store.cancelScheduled())

async function syncScope() {
  store.datasetId = ctx.dataset
  if (!ctx.dataset || !ctx.version) { store.setScope([]); return }
  await items.fetchItems()
  store.setScope(scopeUris.value)
  await Promise.all([store.loadDropped(ctx.version, isCleanVersion.value), store.loadHistory()])
}

watch(() => [ctx.dataset, ctx.version], () => { void syncScope() }, { immediate: true })
</script>

<template>
  <div class="clean" :data-scope="scopeUris.length">
    <OpPanel />
    <PreviewPane v-if="scopeUris.length" />
    <MEmpty
      v-else
      class="clean__noscope"
      text="当前版本没有可选条目：先在管理台建集或切换到有数据的版本"
    />
    <ExecPanel />
  </div>
</template>

<style scoped>
.clean { flex: 1; min-height: 0; display: flex; align-items: stretch; background: var(--bg-base); }
.clean__noscope { flex: 1; border-right: 1px solid var(--border); }
</style>

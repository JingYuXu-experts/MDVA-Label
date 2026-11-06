<script setup lang="ts">
/**
 * V4 增强 `/augment`（UI规格 §5.4 rev6）：左 300 配方面板 / 中央预览（A 级）/ 右 300 执行与谱系。
 *
 * 作用范围 = 当前数据集+版本的条目全集（与 /api/items 同源）；
 * 示例条目优先取审核队列第一条，队列为空回退条目列表第一条（§5.4 交互流 1）。
 * 切到增强产出的版本时自动拉谱系，B 级双联就在右栏行展开处。
 */
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import RecipePanel from '@/components/augment/RecipePanel.vue'
import PreviewPane from '@/components/augment/PreviewPane.vue'
import RunPanel from '@/components/augment/RunPanel.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import * as api from '@/api/endpoints'
import { useAugmentStore } from '@/stores/augment'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore } from '@/stores/items'

const store = useAugmentStore()
const ctx = useCtxStore()
const items = useItemsStore()

const currentVersion = computed(() => ctx.versions.find(v => v.id === ctx.version) || null)
const versionOp = computed(() => String(currentVersion.value?.op || ''))
const isAugmentVersion = computed(() => versionOp.value.startsWith('augment'))

onMounted(async () => {
  await store.loadMeta()
  await syncScope()
})

onBeforeUnmount(() => store.cancelScheduled())

/** 队列第一条：审核队列为空（新集未提交）时回退条目列表第一条，界面按实际取到的那条显示 */
async function pickExample(uris: string[]): Promise<string> {
  if (!uris.length || !ctx.version) return ''
  try {
    const resp = await api.getQueue(ctx.version, ctx.role)
    const first = (resp.queue || []).find(u => uris.includes(u))
    if (first) return first
  } catch {
    // 队列读不出来不阻塞：回退条目列表
  }
  return uris[0]
}

async function syncScope() {
  if (!ctx.dataset || !ctx.version) {
    store.setScope([])
    store.category = ''
    await store.loadLineage('')
    return
  }
  store.category = String(ctx.currentDataset?.meta?.category || '')
  await items.fetchItems()
  const uris = items.items.map(i => i.uri)
  store.setScope(uris)
  store.setExample(await pickExample(uris))
  /** 谱系只在增强产出的版本上有意义：判据取后端版本 op，前端不猜 */
  await store.loadLineage(isAugmentVersion.value ? ctx.version : '')
}

watch(() => [ctx.dataset, ctx.version], () => { void syncScope() })
</script>

<template>
  <div
    class="augment"
    :data-scope="store.scope.length"
    :data-category="store.category"
    :data-version-op="versionOp"
    :data-example="store.exampleUri"
  >
    <RecipePanel />
    <PreviewPane v-if="store.scope.length" />
    <MEmpty
      v-else
      class="augment__noscope"
      text="当前版本没有可选条目：先在管理台建集或切换到有数据的版本"
    />
    <RunPanel />
  </div>
</template>

<style scoped>
.augment { flex: 1; min-height: 0; display: flex; align-items: stretch; background: var(--bg-base); }
.augment__noscope { flex: 1; border-right: 1px solid var(--border); }
</style>

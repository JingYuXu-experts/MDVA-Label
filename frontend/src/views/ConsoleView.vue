<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { RefreshCw, Plus, LayoutGrid, List, Images, PackagePlus } from 'lucide-vue-next'
import StatCard from '@/components/ui/StatCard.vue'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MInput from '@/components/ui/MInput.vue'
import MButton from '@/components/ui/MButton.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MTable, { type TableColumn } from '@/components/ui/MTable.vue'
import ThumbGrid from '@/components/console/ThumbGrid.vue'
import ItemDetailPanel from '@/components/console/ItemDetailPanel.vue'
import CreateDatasetDialog from '@/components/console/CreateDatasetDialog.vue'
import CollectDialog from '@/components/console/CollectDialog.vue'
import MosaicDialog from '@/components/console/MosaicDialog.vue'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore, type StateBucket } from '@/stores/items'
import { useCanvasStore } from '@/stores/canvas'
import * as api from '@/api/endpoints'
import type { StatsResp, ItemRow } from '@/api/types'

const ctx = useCtxStore()
const itemsStore = useItemsStore()
const canvas = useCanvasStore()
const router = useRouter()

const stats = ref<StatsResp | null>(null)
const createOpen = ref(false)
const collectOpen = ref(false)
const mosaicOpen = ref(false)
const collectUris = ref<string[]>([])
const activeUri = ref('')
const viewMode = ref<'grid' | 'list'>('grid')
const page = ref(1)
const pageSize = ref(120)

const activeItem = computed(() =>
  itemsStore.items.find(i => i.uri === activeUri.value) || null)

const bucketOptions = computed(() => {
  const c = itemsStore.bucketCounts
  const defs: { value: StateBucket; label: string }[] = [
    { value: '', label: `全部 ${c['']}` },
    { value: 'unlabeled', label: `未标注 ${c.unlabeled}` },
    { value: 'draft', label: `草稿 ${c.draft}` },
    { value: 'submitted', label: `待审 ${c.submitted}` },
    { value: 'approved', label: `已批 ${c.approved}` },
  ]
  return defs
})

const modalityOptions = computed(() => [
  { value: '', label: '全部模态' },
  ...itemsStore.modalities.map(m => ({ value: m, label: m })),
])

const pageCount = computed(() =>
  Math.max(1, Math.ceil(itemsStore.filtered.length / pageSize.value)))
const paged = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return itemsStore.filtered.slice(start, start + pageSize.value)
})
const pageRange = computed(() => {
  if (!itemsStore.filtered.length) return '0'
  const start = (page.value - 1) * pageSize.value + 1
  return `${start}–${Math.min(page.value * pageSize.value, itemsStore.filtered.length)} / ${itemsStore.filtered.length}`
})

const LIST_COLUMNS: TableColumn<ItemRow>[] = [
  { key: 'name', label: '名称', sortable: true, width: '220px' },
  { key: 'uri', label: 'uri', sortable: true, mono: true },
  { key: 'modality', label: '模态', sortable: true, width: '80px' },
  { key: 'n_annotations', label: '标注数', sortable: true, align: 'right', width: '72px' },
]

watch(pageSize, () => { page.value = 1 })
watch(() => itemsStore.filtered.length, () => {
  if (page.value > pageCount.value) page.value = 1
})

async function fetchStats() {
  if (!ctx.dataset || !ctx.version) { stats.value = null; return }
  try {
    stats.value = await api.getStats(ctx.dataset, ctx.version)
  } catch {
    stats.value = null
  }
}

async function refreshAll() {
  await Promise.all([itemsStore.fetchItems(), fetchStats()])
}

watch(() => [ctx.dataset, ctx.version], () => {
  activeUri.value = ''
  itemsStore.reset()
  page.value = 1
  void refreshAll()
}, { immediate: true })

// 模态筛选走服务端参数（接口映射 §5.1），变更后重拉
watch(() => itemsStore.modalityFilter, () => { void itemsStore.fetchItems() })

function onPick(uri: string) {
  activeUri.value = uri
}

function onAnnotate(uri: string) {
  canvas.openItem(uri)
  void router.push({ path: '/annotate', query: router.currentRoute.value.query })
}

function onCollectSingle(uri: string) {
  collectUris.value = [uri]
  collectOpen.value = true
}

function onCollectSelected() {
  collectUris.value = itemsStore.selectedList.map(i => i.uri)
  collectOpen.value = true
}

function onMosaic() {
  mosaicOpen.value = true
}

async function onCreated(datasetId: string) {
  await ctx.loadDatasets()
  await ctx.setDataset(datasetId)
  await refreshAll()
}

function onRowClick(row: ItemRow) {
  onPick(row.uri)
}
</script>

<template>
  <div class="console">
    <!-- 统计条 -->
    <section class="statsbar" aria-label="统计">
      <StatCard label="条目总数" :value="itemsStore.bucketCounts['']"
        :hint="stats ? `磁盘 ${stats.n_items_on_disk} · 已标 ${Math.round(stats.labeled_ratio * 100)}%` : ''" />
      <StatCard label="未标注" :value="itemsStore.bucketCounts.unlabeled" />
      <StatCard label="草稿" :value="itemsStore.bucketCounts.draft" />
      <StatCard label="待审" :value="itemsStore.bucketCounts.submitted" tone="warning" />
      <StatCard label="已批" :value="itemsStore.bucketCounts.approved" tone="success" />
    </section>

    <!-- 工具栏 -->
    <section class="toolbar" aria-label="筛选与动作">
      <SegmentedControl v-model="itemsStore.stateFilter" :options="bucketOptions"
        aria-label="状态筛选" />
      <MSelect v-model="itemsStore.modalityFilter" :options="modalityOptions" width="110px"
        aria-label="模态筛选" />
      <MInput v-model="itemsStore.search" placeholder="搜索 uri 子串…" class="toolbar__search" />
      <MButton variant="ghost" size="sm" title="刷新" :loading="itemsStore.loading"
        @click="refreshAll">
        <RefreshCw :size="14" aria-hidden="true" />
      </MButton>
      <span class="toolbar__spacer" />
      <MButton v-if="itemsStore.selected.size >= 2" variant="secondary" size="sm"
        @click="onMosaic">
        <Images :size="14" aria-hidden="true" /> 对比拼图（{{ itemsStore.selected.size }}）
      </MButton>
      <MButton v-if="itemsStore.selected.size >= 1" variant="secondary" size="sm"
        @click="onCollectSelected">
        <PackagePlus :size="14" aria-hidden="true" /> 收录到…（{{ itemsStore.selected.size }}）
      </MButton>
      <MButton variant="ghost" size="sm" :title="viewMode === 'grid' ? '切换列表' : '切换网格'"
        @click="viewMode = viewMode === 'grid' ? 'list' : 'grid'">
        <component :is="viewMode === 'grid' ? List : LayoutGrid" :size="14" aria-hidden="true" />
      </MButton>
      <MButton variant="primary" size="sm" @click="createOpen = true">
        <Plus :size="14" aria-hidden="true" /> 新建数据集
      </MButton>
    </section>

    <!-- 主区：左列表 + 右详情 -->
    <div class="body">
      <section class="listpane">
        <p v-if="itemsStore.error" class="listpane__error" role="alert">
          {{ itemsStore.error }}
          <MButton variant="secondary" size="sm" @click="itemsStore.fetchItems()">重试</MButton>
        </p>
        <MSkeleton :show="itemsStore.loading && !itemsStore.items.length" variant="grid" :count="12" />
        <template v-if="!itemsStore.loading || itemsStore.items.length">
          <MEmpty v-if="!ctx.dataset" text="还没有数据集：点右上角「新建数据集」，先只读探测目录再建集。"
            action-label="新建数据集" @action="createOpen = true" />
          <MEmpty v-else-if="!itemsStore.items.length"
            text="该数据集暂无条目，检查 root 目录后点顶栏重扫。" />
          <MEmpty v-else-if="!itemsStore.filtered.length"
            text="当前筛选没有命中条目，放宽状态/模态/搜索条件试试。" />
          <ThumbGrid v-else-if="viewMode === 'grid'" :items="paged" :active-uri="activeUri"
            @pick="onPick" />
          <MTable v-else :rows="paged" :columns="LIST_COLUMNS" :row-key="(row) => row.uri"
            selectable :selected-keys="itemsStore.selected" :active-key="activeUri"
            @update:selected-keys="itemsStore.selected = $event" @row-click="onRowClick" />
        </template>
      </section>
      <ItemDetailPanel :item="activeItem" @annotate="onAnnotate" @collect="onCollectSingle" />
    </div>

    <!-- 分页 -->
    <footer class="paging">
      <MButton variant="ghost" size="sm" :disabled="page <= 1" @click="page -= 1">上一页</MButton>
      <span class="paging__info num">第 {{ page }}/{{ pageCount }} 页 · {{ pageRange }}</span>
      <MButton variant="ghost" size="sm" :disabled="page >= pageCount" @click="page += 1">下一页</MButton>
      <MSelect v-model="pageSize" :options="[
        { value: 60, label: '60/页' }, { value: 120, label: '120/页' }, { value: 240, label: '240/页' },
      ]" width="90px" aria-label="每页条数" />
      <span v-if="itemsStore.selected.size" class="paging__sel">
        已选 {{ itemsStore.selected.size }} 条
        <MButton variant="ghost" size="sm" @click="itemsStore.clearSelection()">清空</MButton>
      </span>
    </footer>

    <CreateDatasetDialog :open="createOpen" @close="createOpen = false" @created="onCreated" />
    <CollectDialog :open="collectOpen" :uris="collectUris" @close="collectOpen = false" />
    <MosaicDialog :open="mosaicOpen" :uris="itemsStore.selectedList.map(i => i.uri)"
      @close="mosaicOpen = false" />
  </div>
</template>

<style scoped>
.console { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.statsbar {
  display: flex; gap: var(--sp-2); padding: var(--sp-3) var(--sp-3) 0;
  flex: none; flex-wrap: wrap;
}
.toolbar {
  display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-3); flex: none; flex-wrap: wrap;
}
.toolbar__search { width: 180px; }
.toolbar__spacer { flex: 1; }
.body { flex: 1; min-height: 0; display: flex; }
.listpane {
  flex: 1; min-width: 0; min-height: 0; overflow-y: auto;
  display: flex; flex-direction: column;
}
.listpane__error {
  margin: var(--sp-2) var(--sp-3); padding: var(--sp-2);
  color: var(--danger); font-size: var(--fs-ui);
  display: flex; align-items: center; gap: var(--sp-2);
  background: rgba(224, 82, 82, 0.08); border-radius: var(--radius-control);
}
.paging {
  flex: none; display: flex; align-items: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--border);
  background: var(--bg-panel);
}
.paging__info { color: var(--text-secondary); font-size: var(--fs-xs); }
.paging__sel { margin-left: auto; color: var(--text-secondary); display: flex; align-items: center; gap: var(--sp-1); }
</style>

/**
 * items store：条目列表 + 状态筛选 + 选中集 + 缩略图缓存（UI规格 §3.3、§5.1、§8）。
 * JSON no-store：每次进入视图/刷新都重新拉取，不做跨视图内存缓存。
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import * as api from '@/api/endpoints'
import type { ItemRow, ThumbRow } from '@/api/types'
import { useCtxStore } from './ctx'

/** 状态五档（UI规格 §5.1 验收 2）：每条目恰好落入一档，计数之和=全部 */
export type StateBucket = '' | 'unlabeled' | 'draft' | 'submitted' | 'approved'

export function bucketOf(item: ItemRow): Exclude<StateBucket, ''> {
  if (item.n_annotations === 0) return 'unlabeled'
  const s = item.states || {}
  if ((s.draft || 0) > 0 || (s.rejected || 0) > 0) return 'draft'
  if ((s.submitted || 0) > 0) return 'submitted'
  return 'approved'
}

export const useItemsStore = defineStore('items', () => {
  const items = ref<ItemRow[]>([])
  const page = ref(1)
  const pageSize = ref(60)
  const total = ref(0)
  const categoryFilter = ref('')
  const serverCounts = ref<Record<string, number>>({ '': 0, unlabeled: 0, draft: 0, submitted: 0, approved: 0 })
  const serverModalities = ref<string[]>([])
  let generation = 0
  let thumbnailContext = 0
  let lastContext = ""
  const loading = ref(false)
  const error = ref('')
  const stateFilter = ref<StateBucket>('')
  const modalityFilter = ref('')
  const search = ref('')
  const selected = ref<Set<string>>(new Set())
  /** uri → 缩略图行（含 error 行）；仅当前会话内使用，切数据集清空 */
  const thumbs = ref<Map<string, ThumbRow>>(new Map())
  const thumbsLoading = ref<Set<string>>(new Set())

  let controller: AbortController | null = null

  const modalities = computed(() => serverModalities.value)
  const filtered = computed(() => items.value)
  const bucketCounts = computed(() => serverCounts.value)

  const selectedList = computed(() => [...selected.value].map(uri => ({ uri })))

  async function fetchItems() {
    const ctx = useCtxStore()
    const context = `${ctx.ws}|${ctx.dataset}|${ctx.version}`
    if (context !== lastContext) { reset(); lastContext = context }
    const run = ++generation
    if (!ctx.dataset || !ctx.version) { reset(); return }
    // 取消在途请求，防旧响应覆盖新状态（UI规格 §3.2-6）
    controller?.abort()
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const resp = await api.getItems(ctx.dataset, ctx.version,
        { modality: modalityFilter.value, state: stateFilter.value, search: search.value, category: categoryFilter.value, page: page.value, page_size: pageSize.value }, controller.signal)
      if (run !== generation) return
      total.value = resp.total
      page.value = resp.page
      serverCounts.value = resp.counts
      serverModalities.value = resp.modalities
      items.value = resp.items
      // Selection persists across pages; changing dataset/version clears it above.
    } catch (err) {
      if (run !== generation || (err as Error).name === 'AbortError') return
      error.value = err instanceof Error ? err.message : String(err)
      // 请求失败保留旧数据 + 重试（UI规格 §5.1 状态）
    } finally {
      if (run === generation) loading.value = false
    }
  }

  /** 可视区批量缩略图：单批 ≤32 uris（UI规格 §8），cell 显式 160,100（§5.1） */
  async function fetchThumbs(uris: string[], signal?: AbortSignal) {
    const run = thumbnailContext
    const pending = uris.filter(u => !thumbs.value.has(u) && !thumbsLoading.value.has(u))
    if (!pending.length) return
    for (const u of pending) thumbsLoading.value.add(u)
    thumbsLoading.value = new Set(thumbsLoading.value)
    try {
      for (let i = 0; i < pending.length; i += 32) {
        const batch = pending.slice(i, i + 32)
        const resp = await api.getThumbs(batch, [160, 100], 0, signal)
        if (run !== thumbnailContext) return
        for (const row of resp.thumbs) thumbs.value.set(row.uri, row)
        thumbs.value = new Map(thumbs.value)
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        // 坏批不阻塞：留空由调用方重试
      }
    } finally {
      if (run !== thumbnailContext) return
      for (const u of pending) thumbsLoading.value.delete(u)
      thumbsLoading.value = new Set(thumbsLoading.value)
    }
  }

  async function refreshSavedItems(){
    const ctx=useCtxStore()
    lastContext=`${ctx.ws}|${ctx.dataset}|${ctx.version}`
    await fetchItems()
  }

  function toggleSelect(uri: string) {
    if (selected.value.has(uri)) selected.value.delete(uri)
    else selected.value.add(uri)
    selected.value = new Set(selected.value)
  }

  function selectAll(uris: string[]) {
    const all = uris.every(u => selected.value.has(u))
    if (all) for (const u of uris) selected.value.delete(u)
    else for (const u of uris) selected.value.add(u)
    selected.value = new Set(selected.value)
  }

  function clearSelection() {
    selected.value = new Set()
  }

  function reset() {
    generation += 1
    thumbnailContext += 1
    controller?.abort()
    loading.value = false
    total.value = 0
    page.value = 1
    serverCounts.value = { "": 0, unlabeled: 0, draft: 0, submitted: 0, approved: 0 }
    items.value = []
    thumbs.value = new Map()
    thumbsLoading.value = new Set()
    selected.value = new Set()
    error.value = ''
    search.value = ''
    stateFilter.value = ''
    modalityFilter.value = ''
    categoryFilter.value = ''
  }

  return {
    items, loading, error, stateFilter, modalityFilter, search, selected, page, pageSize, total, categoryFilter,serverCounts,
    thumbs, thumbsLoading, modalities, filtered, bucketCounts, selectedList,
    fetchItems, refreshSavedItems, fetchThumbs, toggleSelect, selectAll, clearSelection, reset,
  }
})

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

  const modalities = computed(() => {
    const set = new Set<string>()
    for (const it of items.value) if (it.modality) set.add(String(it.modality))
    return [...set].sort()
  })

  const filtered = computed(() => {
    const kw = search.value.trim().toLowerCase()
    return items.value.filter(it => {
      if (stateFilter.value && bucketOf(it) !== stateFilter.value) return false
      if (kw && !it.uri.toLowerCase().includes(kw)) return false
      return true
    })
  })

  /** 五档计数（含全部），供统计条与筛选自洽验收 */
  const bucketCounts = computed(() => {
    const counts: Record<string, number> = { '': items.value.length, unlabeled: 0, draft: 0, submitted: 0, approved: 0 }
    for (const it of items.value) counts[bucketOf(it)] += 1
    return counts
  })

  const selectedList = computed(() => items.value.filter(it => selected.value.has(it.uri)))

  async function fetchItems() {
    const ctx = useCtxStore()
    if (!ctx.dataset || !ctx.version) { items.value = []; return }
    // 取消在途请求，防旧响应覆盖新状态（UI规格 §3.2-6）
    controller?.abort()
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const resp = await api.getItems(ctx.dataset, ctx.version,
        { modality: modalityFilter.value }, controller.signal)
      items.value = resp.items
      // 清掉已不在列表中的选中项
      const uris = new Set(resp.items.map(i => i.uri))
      for (const uri of [...selected.value]) if (!uris.has(uri)) selected.value.delete(uri)
      selected.value = new Set(selected.value)
    } catch (err) {
      if ((err as Error).name === 'AbortError') return
      error.value = err instanceof Error ? err.message : String(err)
      // 请求失败保留旧数据 + 重试（UI规格 §5.1 状态）
    } finally {
      loading.value = false
    }
  }

  /** 可视区批量缩略图：单批 ≤32 uris（UI规格 §8），cell 显式 160,100（§5.1） */
  async function fetchThumbs(uris: string[], signal?: AbortSignal) {
    const pending = uris.filter(u => !thumbs.value.has(u) && !thumbsLoading.value.has(u))
    if (!pending.length) return
    for (const u of pending) thumbsLoading.value.add(u)
    thumbsLoading.value = new Set(thumbsLoading.value)
    try {
      for (let i = 0; i < pending.length; i += 32) {
        const batch = pending.slice(i, i + 32)
        const resp = await api.getThumbs(batch, [160, 100], 0, signal)
        for (const row of resp.thumbs) thumbs.value.set(row.uri, row)
        thumbs.value = new Map(thumbs.value)
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        // 坏批不阻塞：留空由调用方重试
      }
    } finally {
      for (const u of pending) thumbsLoading.value.delete(u)
      thumbsLoading.value = new Set(thumbsLoading.value)
    }
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
    controller?.abort()
    items.value = []
    thumbs.value = new Map()
    thumbsLoading.value = new Set()
    selected.value = new Set()
    error.value = ''
    search.value = ''
    stateFilter.value = ''
    modalityFilter.value = ''
  }

  return {
    items, loading, error, stateFilter, modalityFilter, search, selected,
    thumbs, thumbsLoading, modalities, filtered, bucketCounts, selectedList,
    fetchItems, fetchThumbs, toggleSelect, selectAll, clearSelection, reset,
  }
})

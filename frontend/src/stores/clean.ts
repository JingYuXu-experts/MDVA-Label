/**
 * 清洗视图 store（UI规格 §5.3）。
 *
 * 两条硬口径：
 * 1) 预览与执行同源——界面数字全部来自 /api/clean/preview（后端与 apply 走同一条
 *    evaluate()），前端绝不自己估数；参数变更后 500ms 防抖自动刷新，且防抖是尾沿触发，
 *    连续拖动时最后一次值必达（rev3 同型手势断言）。
 * 2) 原件不动——本 store 没有任何"删除原件"的入口：apply 只产新版本，rollback 只删
 *    后端登记在册且字节对得上的产物，删除语义完全交给后端裁决并原样展示原因。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/endpoints'
import type { CleanApplyResp, CleanOpMeta, CleanPreviewResp, DroppedRow, TaskRow } from '@/api/types'
import { useTasksStore } from './tasks'
import { useToast } from './toast'

const PREVIEW_DEBOUNCE = 500

export interface OpState {
  on: boolean
  params: Record<string, unknown>
}

export const useCleanStore = defineStore('clean', () => {
  const tasks = useTasksStore()
  const toast = useToast()

  const ops = ref<CleanOpMeta[]>([])
  const opsLoading = ref(false)
  const opsError = ref('')
  /** 勾选与参数：界面唯一真源，算子链顺序按后端元数据顺序 */
  const state = ref<Record<string, OpState>>({})
  const calibration = ref('')
  const writeTransformed = ref(false)
  const scope = ref<string[]>([])
  /** 历史/丢弃明细按数据集隔离用 */
  const datasetId = ref('')

  const preview = ref<CleanPreviewResp | null>(null)
  const previewing = ref(false)
  const previewError = ref('')
  const previewedAt = ref(0)

  const applying = ref(false)
  const applyError = ref('')
  const lastApply = ref<CleanApplyResp | null>(null)
  const rollingBack = ref(false)
  const rollbackError = ref('')
  const lastRollback = ref<Record<string, unknown> | null>(null)

  const history = ref<TaskRow[]>([])
  const historyLoading = ref(false)
  const dropped = ref<DroppedRow[]>([])
  const droppedOf = ref('')

  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let inflightId = ''
  /** 预览请求的代际号 + 上一发的中断控制器：慢响应晚到必须作废，否则界面数字与参数不同源 */
  let previewSeq = 0
  let previewController: AbortController | null = null

  const selectedOps = computed(() => ops.value.filter(o => state.value[o.name]?.on))
  /** 契约 §5.7 的 specs：[{name, params}] */
  const specs = computed(() => selectedOps.value.map(o => ({
    name: o.name,
    params: { ...(state.value[o.name]?.params || {}) },
  })))
  const hasSelection = computed(() => selectedOps.value.length > 0)
  /** 数字口径：移除条数 / 参与条数（预览与执行同一来源，不做任何前端换算） */
  const stat = computed(() => {
    const p = preview.value
    return {
      total: p?.n_items ?? scope.value.length,
      removed: p?.n_dropped ?? 0,
      kept: p?.n_kept ?? 0,
      transforms: p?.n_transforms ?? 0,
      groups: p?.groups ?? 0,
      perOp: p?.per_op || {},
    }
  })
  /** 命中列表：契约 v1.3 的 summary 不含 uri 清单，后端补 dropped 键即自动可用 */
  const hitList = computed<DroppedRow[]>(() => preview.value?.dropped || [])

  function initFromOps() {
    const next: Record<string, OpState> = {}
    for (const o of ops.value) {
      const prev = state.value[o.name]
      const params: Record<string, unknown> = {}
      for (const [k, spec] of Object.entries(o.schema || {})) {
        params[k] = prev?.params && k in prev.params ? prev.params[k] : spec.default
      }
      next[o.name] = { on: prev?.on ?? false, params }
    }
    state.value = next
  }

  async function loadOps() {
    opsLoading.value = true
    opsError.value = ''
    try {
      const resp = await api.getOperators('clean')
      const list = (resp as { ops?: CleanOpMeta[] }).ops || []
      ops.value = list
      initFromOps()
    } catch (err) {
      opsError.value = err instanceof Error ? err.message : String(err)
    } finally {
      opsLoading.value = false
    }
  }

  function setParam(name: string, key: string, value: unknown) {
    const s = state.value[name]
    if (!s) return
    s.params[key] = value
    schedulePreview()
  }

  function toggleOp(name: string, on: boolean) {
    const s = state.value[name]
    if (!s) return
    s.on = on
    schedulePreview()
  }

  function setScope(uris: string[]) {
    scope.value = uris
    schedulePreview()
  }

  /** 500ms 尾沿防抖：连续拖动滑块只在停手后发一次，且一定是最后一次的值 */
  function schedulePreview() {
    if (debounceTimer) clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => {
      debounceTimer = null
      void runPreview()
    }, PREVIEW_DEBOUNCE)
  }

  function cancelScheduled() {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
  }

  async function runPreview() {
    if (!hasSelection.value || !scope.value.length) {
      preview.value = null
      previewError.value = ''
      previewSeq++                      // 作废在途响应，否则清空后旧数字会回灌
      return
    }
    const seq = ++previewSeq
    previewController?.abort()          // 只关心最后一次参数下的预览
    previewController = new AbortController()
    previewing.value = true
    previewError.value = ''
    try {
      const resp = await api.cleanPreview(scope.value, specs.value, calibration.value,
        previewController.signal)
      if (seq !== previewSeq) return    // 乱序返回：旧预览晚到，丢弃，不覆盖新数字
      preview.value = resp
      previewedAt.value = Date.now()
    } catch (err) {
      if (seq !== previewSeq) return
      if ((err as Error)?.name === 'AbortError') return   // 被更新的预览取代，不是失败
      previewError.value = err instanceof Error ? err.message : String(err)
      preview.value = null
    } finally {
      if (seq === previewSeq) previewing.value = false
    }
  }

  async function loadHistory() {
    historyLoading.value = true
    try {
      /** 历史必须按数据集隔离：跨集的同 kind 记录会让回退目标指向别集的产物 */
      history.value = (await api.getTasks('clean', datasetId.value)).tasks
    } catch (err) {
      toast.warning(`执行历史读不出来：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      historyLoading.value = false
    }
  }

  /** 丢弃明细只在当前版本确实是清洗版本时有意义（后端从版本 stats 读回） */
  async function loadDropped(versionId: string, isCleanVersion: boolean) {
    if (!isCleanVersion) { dropped.value = []; droppedOf.value = ''; return }
    try {
      dropped.value = (await api.cleanDropped(versionId)).dropped
      droppedOf.value = versionId
    } catch (err) {
      dropped.value = []
      droppedOf.value = ''
      toast.warning(`丢弃明细读不出来：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  /** 执行：同步长请求（§3.2-5），version 显式传（§2.8），按钮 loading + 可取消 + 禁重复提交 */
  async function apply(version: string, author: string): Promise<string> {
    if (!hasSelection.value || applying.value || !version) return ''
    applying.value = true
    applyError.value = ''
    const handle = tasks.begin('clean', `清洗执行（${selectedOps.value.map(o => o.name).join(' → ')}）`)
    inflightId = handle.id
    try {
      const resp = await api.runCleanApply(
        version, specs.value, scope.value, calibration.value || null,
        writeTransformed.value, author, handle.signal) as CleanApplyResp
      lastApply.value = resp
      const newVersion = String(resp.version_id || resp.version || '')
      toast.success(`已产新版本 ${newVersion}，移除 ${resp.summary?.n_dropped ?? 0} 条，原件未改动`)
      await Promise.all([loadHistory(), runPreview()])
      return newVersion
    } catch (err) {
      const e = err as Error & { detail?: string; status?: number }
      applyError.value = e?.detail || e?.message || String(err)
      if (e?.name !== 'AbortError') toast.danger(`清洗未执行：${applyError.value}`)
      return ''
    } finally {
      handle.end()
      inflightId = ''
      applying.value = false
    }
  }

  /** 回退：danger 动作，语义由后端裁决，前端只如实展示 */
  async function rollback(versionId: string): Promise<string> {
    if (!versionId || rollingBack.value) return ''
    rollingBack.value = true
    rollbackError.value = ''
    const handle = tasks.begin('clean', `回退清洗版本 ${versionId}`)
    inflightId = handle.id
    try {
      const resp = await api.cleanRollback(versionId, handle.signal) as unknown as Record<string, unknown>
      lastRollback.value = resp
      const skipped = (resp.skipped_files as unknown[]) || []
      toast.success(`已回退：删除 ${((resp.removed_files as unknown[]) || []).length} 个产物`
        + (skipped.length ? `，${skipped.length} 个按规则未删` : '')
        + `，标注保留 ${resp.annotations_kept ?? 0} 条`)
      /** 回退会产 undo_<op> 版本（父版本=被回退版本的 parent）；把该落点交回视图，
       *  由视图切版本 → 版本列表与条目列表即时反映，不需要刷新页面（§5.3 验收 3） */
      return String(resp.new_version || resp.restored || '')
    } catch (err) {
      const e = err as Error & { detail?: string }
      rollbackError.value = e?.detail || e?.message || String(err)
      toast.danger(`回退失败：${rollbackError.value}`)
      return ''
    } finally {
      handle.end()
      inflightId = ''
      rollingBack.value = false
    }
  }

  /** 取消本视图在途的长请求（同步阻塞语义下唯一可用的中断口） */
  function cancelInflight() {
    if (inflightId) tasks.cancel(inflightId)
  }

  function reset() {
    cancelScheduled()
    preview.value = null
    previewedAt.value = 0
    lastApply.value = null
    lastRollback.value = null
    dropped.value = []
    droppedOf.value = ''
  }

  return {
    ops, opsLoading, opsError, state, calibration, writeTransformed, scope, datasetId,
    preview, previewing, previewError, previewedAt,
    applying, applyError, lastApply, rollingBack, rollbackError, lastRollback,
    history, historyLoading, dropped, droppedOf,
    selectedOps, specs, hasSelection, stat, hitList,
    loadOps, setParam, toggleOp, setScope, schedulePreview, cancelScheduled, runPreview,
    loadHistory, loadDropped, apply, rollback, cancelInflight, reset,
  }
})

/**
 * 增强视图 store（UI规格 §5.4，rev6 两级渐进口径）。
 *
 * 三条硬口径：
 * 1) 不估数——界面出现的每个条数都来自后端（run 的 summary、lineage 的行、replay 的
 *    n_jobs/n_stored）。执行前"会产多少副本"是服务端 plan() 的结果，前端不按 factor×条数
 *    估算，因为没有 dry-run 端点（rev6 A 级）。
 * 2) 执行前必过 check——RunButton 由 checkRan 门控；参数任何改动 500ms 防抖自动重跑 check
 *    （尾沿触发，最后一次值必达），并带代际号 + abort：慢响应晚到一律作废，否则告警与配方不同源。
 * 3) 可复现的前提是 seed 可见——seed 进界面即为随机值并显示实际数字，replay 只报
 *    "参数链逐条一致/不一致"（引擎校验的是链不是像素，rev6 措辞纠正）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/endpoints'
import type {
  AugLineageRow, AugOpMeta, AugPreset, AugRecipe, AugReplayResp, AugRunResp, AugSuggestResp,
} from '@/api/types'
import { useTasksStore } from './tasks'
import { useToast } from './toast'

const CHECK_DEBOUNCE = 500

export interface AugOpState {
  on: boolean
  params: Record<string, unknown>
}

/** 随机 seed：预设里的 seed 是 0（固定序列），界面默认给一个随机值并把实际数字显示出来 */
function randomSeed(): number {
  return Math.floor(Math.random() * 100_000)
}

export const useAugmentStore = defineStore('augment', () => {
  const tasks = useTasksStore()
  const toast = useToast()

  // ---- 元数据（GET /api/augment/recipes：三 Tab 的唯一数据源）----
  const ops = ref<AugOpMeta[]>([])
  const presets = ref<AugPreset[]>([])
  const taskPresets = ref<Record<string, string>>({})
  const metaLoading = ref(false)
  const metaError = ref('')

  // ---- 配方（界面唯一真源；算子链顺序按后端元数据顺序）----
  const tab = ref<'preset' | 'suggest' | 'custom'>('preset')
  const state = ref<Record<string, AugOpState>>({})
  /**
   * 算子链顺序：预设里写的顺序就是执行顺序（flip→rotate90 与 rotate90→flip 不等价），
   * 所以界面按配方的原始顺序发链；自定义勾选新算子时追加到链尾。
   */
  const order = ref<string[]>([])
  const seed = ref(randomSeed())
  const factor = ref(1)
  const limit = ref<number | null>(null)
  const sliceIndex = ref(0)
  /** 配方来源预设（用于 PresetCard 高亮与"已在预设基础上改动"标注） */
  const sourceName = ref('')
  const edited = ref(false)
  const suggested = ref<AugSuggestResp | null>(null)
  const suggesting = ref(false)
  const suggestError = ref('')

  // ---- 作用范围与示例条目 ----
  const scope = ref<string[]>([])
  const category = ref('')
  const exampleUri = ref('')

  // ---- 告警检查 ----
  const warnings = ref<string[]>([])
  const checking = ref(false)
  const checkError = ref('')
  const checkedAt = ref(0)
  const checkRan = ref(false)

  // ---- 执行 / 谱系 / 复现 ----
  const running = ref(false)
  const runError = ref('')
  const lastRun = ref<AugRunResp | null>(null)
  const lineage = ref<AugLineageRow[]>([])
  const lineageLoading = ref(false)
  const lineageError = ref('')
  const lineageOf = ref('')
  const openRow = ref(-1)
  const replay = ref<AugReplayResp | null>(null)
  const replaying = ref(false)
  const replayError = ref('')

  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let inflightId = ''
  let checkSeq = 0
  let checkController: AbortController | null = null

  const selectedOps = computed(() => order.value
    .map(name => ops.value.find(o => o.name === name))
    .filter((o): o is AugOpMeta => !!o && !!state.value[o.name]?.on))
  const hasRecipe = computed(() => selectedOps.value.length > 0)

  /** 线上配方：与后端 lineage.Recipe 同形（seed/factor/ops/limit/index） */
  const recipe = computed<AugRecipe>(() => ({
    seed: Number(seed.value) || 0,
    factor: Number(factor.value) || 1,
    ops: selectedOps.value.map(o => ({ name: o.name, params: { ...(state.value[o.name]?.params || {}) } })),
    limit: limit.value === null || limit.value === 0 ? null : Number(limit.value),
    index: Number(sliceIndex.value) || 0,
  }))

  /** A 级参数链：配方层的算子与参数（mono 行），旁注"逐条抽样值执行后可查" */
  const chain = computed(() => recipe.value.ops.map(o => ({
    name: o.name,
    params: Object.entries(o.params || {})
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : String(v)}`)
      .join('  '),
  })))

  /** RunButton 门控：必须先有配方、有条目，且 check 真跑成过（§5.4 验收 2） */
  const canRun = computed(() => hasRecipe.value && scope.value.length > 0
    && checkRan.value && !running.value)

  const sourceTitle = computed(() => {
    if (!sourceName.value) return ''
    const p = presets.value.find(x => x.name === sourceName.value)
    return p ? p.title : sourceName.value
  })

  const lineageCount = computed(() => lineage.value.length)

  function initFromOps() {
    const next: Record<string, AugOpState> = {}
    for (const o of ops.value) {
      const prev = state.value[o.name]
      const params: Record<string, unknown> = {}
      for (const [k, spec] of Object.entries(o.schema || {})) {
        params[k] = prev?.params && k in prev.params ? prev.params[k] : spec.default
      }
      next[o.name] = { on: prev?.on ?? false, params }
    }
    state.value = next
    // 链顺序归一：丢掉已不存在的算子名，再把面板里开着但没进链的算子按元数据顺序补上
    const known = new Set(ops.value.map(o => o.name))
    const kept = order.value.filter(n => known.has(n))
    for (const o of ops.value) {
      if (next[o.name]?.on && !kept.includes(o.name)) kept.push(o.name)
    }
    order.value = kept
  }

  async function loadMeta() {
    metaLoading.value = true
    metaError.value = ''
    try {
      const resp = await api.getAugmentRecipes()
      ops.value = resp.ops || []
      presets.value = resp.presets || []
      taskPresets.value = resp.task_presets || {}
      initFromOps()
    } catch (err) {
      metaError.value = err instanceof Error ? err.message : String(err)
    } finally {
      metaLoading.value = false
    }
  }

  /** 预设填入配方：算子勾选与参数整体覆盖为预设值，其余算子取消勾选但保留 schema 默认 */
  function applyPreset(preset: AugPreset, markEdited = false) {
    const next: Record<string, AugOpState> = {}
    const wanted = new Map((preset.recipe?.ops || []).map(o => [o.name, o.params || {}]))
    for (const o of ops.value) {
      const params: Record<string, unknown> = {}
      const fromPreset = wanted.get(o.name)
      for (const [k, spec] of Object.entries(o.schema || {})) {
        params[k] = fromPreset && k in fromPreset ? fromPreset[k] : spec.default
      }
      next[o.name] = { on: wanted.has(o.name), params }
    }
    state.value = next
    order.value = (preset.recipe?.ops || [])
      .map(o => o.name)
      .filter((n, i, arr) => ops.value.some(o => o.name === n) && arr.indexOf(n) === i)
    factor.value = Number(preset.recipe?.factor) || 1
    limit.value = preset.recipe?.limit ?? null
    sliceIndex.value = Number(preset.recipe?.index) || 0
    // seed 属"可复现前提"，由界面持有：切预设不重置随机 seed，只把实际值带着走
    sourceName.value = preset.name
    edited.value = markEdited
    scheduleCheck()
  }

  async function runSuggest(datasetId: string) {
    if (suggesting.value) return
    suggesting.value = true
    suggestError.value = ''
    try {
      const resp = await api.augmentSuggest(datasetId)
      suggested.value = resp
      if (resp.preset) applyPreset(resp.preset)
    } catch (err) {
      suggestError.value = err instanceof Error ? err.message : String(err)
      toast.warning(`推荐配方读不出来：${suggestError.value}`)
    } finally {
      suggesting.value = false
    }
  }

  function markEdited() {
    edited.value = true
  }

  function toggleOp(name: string, on: boolean) {
    const s = state.value[name]
    if (!s || s.on === on) return
    s.on = on
    // 关掉只移出链、不删位：再次打开时仍回到原来的链顺序
    if (on && !order.value.includes(name)) order.value = [...order.value, name]
    markEdited()
    scheduleCheck()
  }

  function setParam(name: string, key: string, value: unknown) {
    const s = state.value[name]
    if (!s) return
    s.params[key] = value
    markEdited()
    scheduleCheck()
  }

  function setSeed(v: number) { seed.value = Math.max(0, Math.round(v)); markEdited(); scheduleCheck() }
  function reseed() { seed.value = randomSeed(); markEdited(); scheduleCheck() }
  function setFactor(v: number) { factor.value = Math.max(1, Math.round(v)); markEdited(); scheduleCheck() }
  function setLimit(v: number | null) { limit.value = v; markEdited(); scheduleCheck() }
  function setSliceIndex(v: number) { sliceIndex.value = Math.max(0, Math.round(v)); markEdited(); scheduleCheck() }

  function setScope(uris: string[]) {
    scope.value = uris
    exampleUri.value = uris[0] || ''
    scheduleCheck()
  }

  /** 示例条目：优先审核队列第一条，队列为空回退条目列表第一条（§5.4 交互流 1） */
  function setExample(uri: string) { exampleUri.value = uri || '' }

  /** 500ms 尾沿防抖：连续改参数只在停手后跑一次 check，且一定是最后一次的值 */
  function scheduleCheck() {
    if (debounceTimer) clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => {
      debounceTimer = null
      void runCheck()
    }, CHECK_DEBOUNCE)
  }

  function cancelScheduled() {
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null }
  }

  async function runCheck(): Promise<boolean> {
    if (!hasRecipe.value || !scope.value.length) {
      warnings.value = []
      checkError.value = ''
      checkRan.value = false            // 无配方 = 没检查过，RunButton 保持禁用
      checkedAt.value = 0
      checkSeq++                        // 作废在途响应，避免旧告警回灌
      return false
    }
    const seq = ++checkSeq
    checkController?.abort()
    checkController = new AbortController()
    checking.value = true
    checkError.value = ''
    try {
      const resp = await api.augmentCheck(scope.value, recipe.value, category.value,
        checkController.signal)
      if (seq !== checkSeq) return false
      warnings.value = resp.warnings || []
      checkedAt.value = Date.now()
      checkRan.value = true
      return true
    } catch (err) {
      if (seq !== checkSeq) return false
      if ((err as Error)?.name === 'AbortError') return false
      const e = err as Error & { detail?: string }
      checkError.value = e?.detail || e?.message || String(err)
      warnings.value = []
      checkRan.value = false
      return false
    } finally {
      if (seq === checkSeq) checking.value = false
    }
  }

  /** 执行增强：同步长请求（§3.2-5）；原件与父版本一律不动，只产新版本 */
  async function run(version: string, author: string): Promise<string> {
    if (!canRun.value || !version) return ''
    running.value = true
    runError.value = ''
    const handle = tasks.begin('augment', `增强执行（${selectedOps.value.map(o => o.name).join(' → ')}）`)
    inflightId = handle.id
    try {
      const resp = await api.runAugment(version, recipe.value, scope.value, author, handle.signal)
      lastRun.value = resp
      const newVersion = String(resp.version_id || resp.version || '')
      /** Toast 文案按 §5.4 交互流 3，条数取后端 summary，不前端估 */
      toast.success(`新版本 ${newVersion}：${resp.summary?.n_generated ?? (resp.written?.length || 0)} 条，谱系已记录`)
      replay.value = null
      openRow.value = -1
      await loadLineage(newVersion)
      return newVersion
    } catch (err) {
      const e = err as Error & { detail?: string }
      runError.value = e?.detail || e?.message || String(err)
      if ((err as Error)?.name !== 'AbortError') toast.danger(`增强未执行：${runError.value}`)
      return ''
    } finally {
      handle.end()
      inflightId = ''
      running.value = false
    }
  }

  /** 谱系：行=条目，after 帧的 uri 就是这里回的 written（相对工作区根，与 items/frame 同口径） */
  async function loadLineage(versionId: string) {
    if (!versionId) { lineage.value = []; lineageOf.value = ''; openRow.value = -1; return }
    const sameTarget = lineageOf.value === versionId
    lineageLoading.value = true
    lineageError.value = ''
    try {
      lineage.value = (await api.augmentLineage(versionId)).lineage || []
      lineageOf.value = versionId
      // 同一版本的重拉（执行后与切版后各来一次）不打断用户正在看的展开行
      if (!sameTarget) openRow.value = -1
    } catch (err) {
      lineage.value = []
      lineageOf.value = ''
      lineageError.value = err instanceof Error ? err.message : String(err)
      toast.warning(`谱系读不出来：${lineageError.value}`)
    } finally {
      lineageLoading.value = false
    }
  }

  /** 复现验证：校验 seed 谱系链；结论只有"参数链逐条一致/不一致"两种（rev6 禁用"逐字节一致"） */
  async function runReplay(versionId: string) {
    if (!versionId || replaying.value) return
    replaying.value = true
    replayError.value = ''
    const handle = tasks.begin('augment', `复现校验 ${versionId}`)
    inflightId = handle.id
    try {
      replay.value = await api.augmentReplay(versionId, handle.signal)
    } catch (err) {
      const e = err as Error & { detail?: string }
      replayError.value = e?.detail || e?.message || String(err)
      replay.value = null
      toast.danger(`复现校验失败：${replayError.value}`)
    } finally {
      handle.end()
      inflightId = ''
      replaying.value = false
    }
  }

  function toggleRow(idx: number) { openRow.value = openRow.value === idx ? -1 : idx }

  function cancelInflight() {
    if (inflightId) tasks.cancel(inflightId)
  }

  function reset() {
    cancelScheduled()
    checkSeq++
    warnings.value = []
    checkedAt.value = 0
    checkRan.value = false
    lastRun.value = null
    replay.value = null
    lineage.value = []
    lineageOf.value = ''
    openRow.value = -1
  }

  return {
    ops, presets, taskPresets, metaLoading, metaError,
    tab, state, order, seed, factor, limit, sliceIndex, sourceName, edited,
    suggested, suggesting, suggestError,
    scope, category, exampleUri,
    warnings, checking, checkError, checkedAt, checkRan,
    running, runError, lastRun,
    lineage, lineageLoading, lineageError, lineageOf, lineageCount, openRow,
    replay, replaying, replayError,
    recipe, chain, selectedOps, hasRecipe, canRun, sourceTitle,
    loadMeta, applyPreset, runSuggest, toggleOp, setParam,
    setSeed, reseed, setFactor, setLimit, setSliceIndex,
    setScope, setExample, scheduleCheck, cancelScheduled, runCheck,
    run, loadLineage, runReplay, toggleRow, cancelInflight, reset,
  }
})

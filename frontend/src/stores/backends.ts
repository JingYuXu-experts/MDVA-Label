/**
 * 后端（算法替换）视图 store（UI规格 §5.6）。
 *
 * 三条口径：
 * 1) 清单/筛选/参数全部来自 GET /api/backends 的元数据，前端不写死后端名与档位；
 *    筛选下拉的候选值也从"未过滤的全量清单"反推，后端加一个 domain 界面自动多一项。
 * 2) 空态先行（§5.6 验收 1）：没有实测表时推荐/门槛是设计空态，清单与筛选仍完全可用；
 *    note 用后端原文，不自造"暂无数据"以外的解释。
 * 3) 不可用后端（三个深度占位）灰化 + reason 原文，绝不伪装成故障；
 *    它们由后端 rank() 排除在推荐之外，界面不另做过滤。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/endpoints'
import type { BackendRow, BenchRow, GateResp, ProbeResp, RecommendRow } from '@/api/types'
import { useTasksStore } from './tasks'
import { useToast } from './toast'

export const useBackendsStore = defineStore('backends', () => {
  const tasks = useTasksStore()
  const toast = useToast()

  // ---- 清单与筛选 ----
  const probe = ref<ProbeResp | null>(null)
  /** 未过滤全量清单：只用于反推筛选候选值与"总数"口径 */
  const all = ref<BackendRow[]>([])
  const backends = ref<BackendRow[]>([])
  const loading = ref(false)
  const loadError = ref('')
  /** rev13 §5.6：「探测请求失败」与「取不到档位」是两件事，不得共用一句文案互冒充 */
  const probeError = ref('')
  const domain = ref('')
  const task = ref('')
  const modality = ref('')
  const chosen = ref('')

  // ---- 推荐 / 门槛 / 实测 ----
  const recommended = ref<RecommendRow[]>([])
  const hasTable = ref(false)
  const recommendNote = ref('')
  const benchRows = ref<BenchRow[]>([])
  const gate = ref<GateResp | null>(null)
  const threshold = ref(0.90)

  // ---- 实测长任务（分钟级，§3.2-5：loading + indeterminate + 可取消）----
  const benching = ref(false)
  const benchError = ref('')
  const benchTablePath = ref('')
  let benchHandle: { id: string; signal: AbortSignal; end: () => void } | null = null

  const uniq = (vals: unknown[]): string[] => {
    const out: string[] = []
    for (const v of vals) {
      const s = String(v ?? '')
      if (s && s !== '*' && !out.includes(s)) out.push(s)
    }
    return out.sort()
  }

  /** 筛选候选值由元数据反推：domain 用 "*" 表示"不限"，与后端 covers() 语义一致 */
  const domainOpts = computed(() =>
    [{ value: '', label: '* （不限）' }, ...uniq(all.value.flatMap((b) => b.domains || [])).map((v) => ({ value: v, label: v }))])
  const taskOpts = computed(() =>
    [{ value: '', label: '* （不限）' }, ...uniq(all.value.flatMap((b) => b.tasks || [])).map((v) => ({ value: v, label: v }))])
  const modalityOpts = computed(() =>
    [{ value: '', label: '* （不限）' }, ...uniq(all.value.flatMap((b) => b.modalities || [])).map((v) => ({ value: v, label: v }))])

  const chosenRow = computed(() => backends.value.find((b) => b.name === chosen.value)
    || all.value.find((b) => b.name === chosen.value) || null)

  /** n_available 是"筛完还剩几个真能跑"，与"清单条数"分开，免得把占位后端算成可用 */
  const nAvailable = computed(() => backends.value.filter((b) => b.available).length)

  /** 该后端在当前 task 下有没有实测行：决定门槛 Tab 出"无数据"还是出判定 */
  const gateMeasured = computed(() => {
    if (!chosen.value) return false
    return benchRows.value.some((r) => r.backend === chosen.value
      && (!task.value || r.task === task.value))
  })

  async function loadCatalog(): Promise<void> {
    loading.value = true
    loadError.value = ''
    probeError.value = ''
    try {
      const [p, list] = await Promise.all([
        api.getProbe().catch((err) => {
          probeError.value = err instanceof Error ? err.message : String(err)
          return null
        }),
        api.getBackends('', '', ''),
      ])
      probe.value = p
      all.value = list.backends
      backends.value = list.backends
      if (!chosen.value && list.backends.length) chosen.value = list.backends[0].name
    } catch (err) {
      loadError.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  /** 筛选走后端 query（§5.6 验收 4：按 task 筛选不得返回空列表），不在前端二次过滤 */
  async function applyFilters(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      const resp = await api.getBackends(domain.value, task.value, modality.value)
      backends.value = resp.backends
      if (resp.backends.length && !resp.backends.some((b) => b.name === chosen.value)) {
        chosen.value = resp.backends[0].name
      }
    } catch (err) {
      loadError.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  async function loadRecommend(): Promise<void> {
    try {
      const resp = await api.getRecommend(domain.value, task.value, 3)
      recommended.value = resp.recommended
      hasTable.value = !!resp.has_table
      recommendNote.value = resp.note || ''
    } catch (err) {
      toast.danger(`推荐读取失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  async function loadGate(): Promise<void> {
    if (!chosen.value) { gate.value = null; return }
    try {
      gate.value = await api.getGate(chosen.value, task.value, domain.value, threshold.value)
    } catch (err) {
      gate.value = null
      toast.danger(`门槛读取失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  async function loadBench(): Promise<void> {
    try {
      benchRows.value = (await api.getBenchTable()).rows
    } catch (err) {
      // 实测表还没落盘不是故障：空态由 Tab 自己呈现，这里只留一句原文
      benchRows.value = []
      benchError.value = err instanceof Error ? err.message : String(err)
    }
  }

  /** 同步长请求：按钮 loading + indeterminate Progress + 取消；取消不中止后端已开始的落库 */
  async function runBench(root: string, limit = 4, stubs = false): Promise<boolean> {
    if (benching.value) return false
    benching.value = true
    benchError.value = ''
    const handle = tasks.begin('bench', `实测（${root || '当前数据集'}，limit=${limit}）`)
    benchHandle = handle
    try {
      const resp = await api.runBench(root, '', [], limit, stubs, handle.signal)
      const rows = (resp.rows || []) as BenchRow[]
      benchRows.value = rows
      benchTablePath.value = String(resp.table || '')
      toast.info(`实测完成：${rows.length} 行已落表，推荐与门槛已刷新`)
      await Promise.all([loadRecommend(), loadGate()])
      return true
    } catch (err) {
      benchError.value = err instanceof Error ? err.message : String(err)
      toast.danger(`实测失败：${benchError.value}`)
      return false
    } finally {
      handle.end()
      benchHandle = null
      benching.value = false
    }
  }

  function cancelBench(): void {
    if (benchHandle) tasks.cancel(benchHandle.id)
  }

  async function select(name: string): Promise<void> {
    if (chosen.value === name) return
    chosen.value = name
    await loadGate()
  }

  return {
    probe, all, backends, loading, loadError, probeError, domain, task, modality, chosen,
    recommended, hasTable, recommendNote, benchRows, gate, threshold,
    benching, benchError, benchTablePath,
    domainOpts, taskOpts, modalityOpts, chosenRow, nAvailable, gateMeasured,
    loadCatalog, applyFilters, loadRecommend, loadGate, loadBench,
    runBench, cancelBench, select,
  }
})

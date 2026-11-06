/**
 * 导出视图 store（UI规格 §5.5）。
 *
 * 三条硬口径：
 * 1) 表单完全由 GET /api/export/formats 的 schema 生成——格式名、参数名、默认值都不写死；
 *    「划分三滑杆」也不是写死的控件，而是探测 schema 里那个"三段斜杠比例串"参数（split_ratios 形态）
 *    自动升格成滑杆；没有这个参数的格式（如无损 JSON）就如实说明"该格式不接受比例参数"。
 * 2) 划分非法（和≠100%）前端即时拦截、不发请求（§5.5 验收 2）——后端会按比例归一，
 *    静默接受非法值反而让用户以为 60/20/20 真跑出去了。
 * 3) 校验是回读对账，不是自检：ok/issues/counts 全部来自 POST /api/export/validate，
 *    差异明细原样展示，前端不判断"这算不算问题"。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/endpoints'
import type {
  ExportFormatMeta, ExportRunResp, ExportValidateResp, OpParamSpec, TaskRow,
} from '@/api/types'
import { useTasksStore } from './tasks'
import { useToast } from './toast'

export interface ExportParamState {
  /** 有值就显式上送；default 为 null 的可选项留空即不上送（吃后端默认） */
  value: unknown
  text: string
}

/** 三段斜杠比例串（"0.7/0.15/0.15"）是划分参数的结构特征，不靠格式名猜 */
const RATIO_RE = /^\s*0?\.\d+\s*\/\s*0?\.\d+\s*\/\s*0?\.\d+\s*$/
const FOLD_NAMES = ['train', 'val', 'test']

export const useExportStore = defineStore('export', () => {
  const tasks = useTasksStore()
  const toast = useToast()

  const formats = ref<ExportFormatMeta[]>([])
  const formatsLoading = ref(false)
  const formatsError = ref('')
  const defaultOutDir = ref('')
  const chosen = ref('')
  /** 参数表单：key → 值（数组/可空项用 text 承载编辑态） */
  const params = ref<Record<string, ExportParamState>>({})
  /** 划分百分比（与三段比例串一一对应，只在检测到该参数时生效） */
  const split = ref<number[]>([70, 15, 15])
  const splitSeed = ref(0)
  const scripts = ref(false)

  const running = ref(false)
  const runError = ref('')
  const result = ref<ExportRunResp | null>(null)

  const validating = ref(false)
  const validateError = ref('')
  const validated = ref<ExportValidateResp | null>(null)

  const reports = ref<TaskRow[]>([])
  const reportsLoading = ref(false)
  const reportsError = ref('')

  let inflightId = ''

  const currentFormat = computed(() =>
    formats.value.find(f => f.name === chosen.value) || null)
  const schema = computed(() => currentFormat.value?.schema || {})

  /** 划分参数：schema 里形如 "0.7/0.15/0.15" 的字符串参数 */
  const splitKey = computed(() => {
    for (const [k, spec] of Object.entries(schema.value)) {
      if (spec.type === 'string' && typeof spec.default === 'string' && RATIO_RE.test(spec.default)) {
        return k
      }
    }
    return ''
  })
  const hasSplit = computed(() => !!splitKey.value)
  /** 划分随机种子：与 split 同源的整型键（缺这个键就不显示种子控件） */
  const seedKey = computed(() => {
    for (const [k, spec] of Object.entries(schema.value)) {
      if (spec.type === 'integer' && k === 'seed') return k
    }
    return ''
  })
  const splitSum = computed(() => split.value.reduce((a, b) => a + b, 0))
  const splitValid = computed(() => splitSum.value === 100
    && split.value.every(v => Number.isFinite(v) && v >= 0))
  const splitError = computed(() => {
    if (!hasSplit.value || splitValid.value) return ''
    return `三段比例之和是 ${splitSum.value}%，必须等于 100%（现在发出去会被归一，等于没按你填的导）`
  })

  /** 上送参数：split 串由滑杆合成；种子跟 split 走；可空项留空不上送 */
  const payload = computed<Record<string, unknown>>(() => {
    const out: Record<string, unknown> = {}
    for (const [k, spec] of Object.entries(schema.value)) {
      if (k === splitKey.value) {
        out[k] = split.value.map(v => (v / 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '.0'))
          .join('/')
        continue
      }
      if (k === seedKey.value) { out[k] = splitSeed.value; continue }
      const st = params.value[k]
      if (!st) continue
      if (spec.default === null || spec.default === undefined) {
        const raw = String(st.text ?? '').trim()
        if (!raw) continue                     // 留空 = 用后端默认，不伪装成显式值
        if (spec.type === 'array') {
          out[k] = raw.split(/[,，\s]+/).filter(Boolean)
        } else if (spec.type === 'number' || spec.type === 'integer') {
          const n = Number(raw)
          if (Number.isFinite(n)) out[k] = spec.type === 'integer' ? Math.round(n) : n
        } else {
          out[k] = raw
        }
        continue
      }
      out[k] = st.value
    }
    return out
  })

  /** 界面要显示的"划分结果预估"只能来自上次真实导出（前端不自己算折数） */
  const splitOutcome = computed(() => result.value?.split || null)
  const canExport = computed(() => !!chosen.value && !running.value
    && (!hasSplit.value || splitValid.value))
  const exportWhy = computed(() => {
    if (running.value) return '导出进行中'
    if (!chosen.value) return '先选一种导出格式'
    if (splitError.value) return splitError.value
    return ''
  })
  /** 校验目标目录：优先用本次导出的落盘路径，其次用后端默认根（只读、不臆造） */
  const validateDir = computed(() => String(result.value?.out_dir || ''))
  const canValidate = computed(() => !!validateDir.value && !validating.value && !running.value)
  /** 对账明细都在 check 里（后端 {ok, summary, check}），前端不自己判定"算不算问题" */
  const check = computed(() => validated.value?.check || null)
  const checkIssues = computed(() => check.value?.issues || [])
  /** 后端累计条数优先于回传明细长度：MAX_ISSUES 截断时不能把 51 说成 50 */
  const checkIssueCount = computed(() =>
    Math.max(Number(check.value?.n_issues ?? 0), checkIssues.value.length))
  /**
   * scripts 字段里混着两类东西：train.py / train.bat / DATASET.md 的落盘路径，
   * 以及 format / data / task 三个描述值（不是路径）。按"值是否落在导出目录内"分开，
   * 不靠键名写死——后端加一个描述字段也不会被界面当成可复制的文件路径。
   */
  const scriptPaths = computed(() => {
    const s = result.value?.scripts
    const dir = String(result.value?.out_dir || '').replace(/\\/g, '/').replace(/\/+$/, '')
    const pairs = Object.entries(s || {}).filter(([, v]) => typeof v === 'string')
    const inDir = pairs.filter(([, v]) => {
      const rel = String(v).replace(/\\/g, '/')
      return !!dir && (rel === dir || rel.startsWith(dir + '/'))
    })
    return inDir
  })
  /** 非路径的描述值：单独显示，不伪装成文件位置 */
  const scriptNotes = computed(() => {
    const paths = new Set(scriptPaths.value.map(([, v]) => String(v)))
    return Object.entries(result.value?.scripts || {})
      .filter(([, v]) => typeof v === 'string' && !paths.has(String(v)))
  })

  function initParams(meta: ExportFormatMeta) {
    const next: Record<string, ExportParamState> = {}
    for (const [k, spec] of Object.entries(meta.schema || {})) {
      const dflt = spec.default
      next[k] = {
        value: Array.isArray(dflt) ? [...dflt] : (dflt === null || dflt === undefined ? spec.type === 'boolean' : dflt),
        text: Array.isArray(dflt) ? dflt.join(', ') : (dflt === null || dflt === undefined ? '' : String(dflt)),
      }
      if (spec.type === 'array' && !Array.isArray(dflt)) next[k].text = ''
    }
    params.value = next
    // 滑杆初值取 schema 默认串，保证"界面显示 = 将上送"
    const key = Object.entries(meta.schema || {})
      .find(([, s]) => s.type === 'string' && typeof s.default === 'string' && RATIO_RE.test(s.default))
    if (key) {
      split.value = String(key[1].default).split('/').map(x => Math.round(Number(x.trim()) * 100))
    }
    const seed = (meta.schema || {}) as Record<string, OpParamSpec>
    if (seed.seed && seed.seed.type === 'integer') splitSeed.value = Number(seed.seed.default ?? 0)
  }

  async function loadFormats() {
    formatsLoading.value = true
    formatsError.value = ''
    try {
      const resp = await api.getExportFormats()
      formats.value = resp.formats || []
      defaultOutDir.value = String(resp.out_dir || '')
      if (chosen.value && !formats.value.some(f => f.name === chosen.value)) chosen.value = ''
      // 不自动替用户选格式：§5.5 明确"未选格式导出禁用"，导出产物落盘是对外动作，得是主动选择
      if (chosen.value) {
        const meta = formats.value.find(f => f.name === chosen.value)
        if (meta) initParams(meta)
      }
    } catch (err) {
      formatsError.value = err instanceof Error ? err.message : String(err)
    } finally {
      formatsLoading.value = false
    }
  }

  function selectFormat(name: string) {
    const meta = formats.value.find(f => f.name === name)
    if (!meta) return
    chosen.value = name
    initParams(meta)
    validated.value = null
    validateError.value = ''
  }

  function setSplit(idx: number, v: number) {
    const next = [...split.value]
    next[idx] = Math.max(0, Math.round(v))
    split.value = next
  }
  function setSeed(v: number) { splitSeed.value = Math.max(0, Math.round(v)) }
  function setParam(key: string, value: unknown, text = '') {
    const st = params.value[key]
    if (!st) return
    st.value = value
    st.text = text
  }
  function setParamText(key: string, text: string) {
    const st = params.value[key]
    if (st) st.text = text
  }

  /** 导出：同步长请求（§3.2-5）；成功后不自动校验，按 §5.5 "自动建议校验"给一键入口 */
  async function run(version: string) {
    if (!canExport.value || !version) return
    running.value = true
    runError.value = ''
    validated.value = null
    validateError.value = ''
    const handle = tasks.begin('export', `导出 ${chosen.value}`)
    inflightId = handle.id
    try {
      const resp = await api.runExport(version, chosen.value, payload.value, '', scripts.value,
        handle.signal)
      result.value = resp
      toast.success(`已导出 ${resp.format}：${resp.files?.length ?? 0} 项产物落盘，建议立即回读校验`)
      await loadReports(version)
      return resp
    } catch (err) {
      const e = err as Error & { detail?: string }
      runError.value = e?.detail || e?.message || String(err)
      if ((err as Error)?.name !== 'AbortError') toast.danger(`导出失败：${runError.value}`)
      return null
    } finally {
      handle.end()
      inflightId = ''
      running.value = false
    }
  }

  async function validate(version: string, dir = '') {
    const target = dir || validateDir.value
    if (!target || validating.value) return
    validating.value = true
    validateError.value = ''
    const handle = tasks.begin('export', `回读校验 ${target}`)
    inflightId = handle.id
    try {
      validated.value = await api.exportValidate(target, version, handle.signal)
      if (validated.value.ok) toast.success('回读校验通过：产物与版本一致')
      else toast.warning(`回读校验有 ${checkIssueCount.value} 条差异`)
    } catch (err) {
      const e = err as Error & { detail?: string }
      validateError.value = e?.detail || e?.message || String(err)
      validated.value = null
      toast.danger(`回读校验失败：${validateError.value}`)
    } finally {
      handle.end()
      inflightId = ''
      validating.value = false
    }
  }

  async function loadReports(versionId: string) {
    if (!versionId) { reports.value = []; return }
    reportsLoading.value = true
    reportsError.value = ''
    try {
      reports.value = (await api.getExportReport(versionId)).reports
    } catch (err) {
      reportsError.value = err instanceof Error ? err.message : String(err)
      reports.value = []
    } finally {
      reportsLoading.value = false
    }
  }

  /** 落盘路径复制：离线 http 下 clipboard 可能不可用，失败就退回手动选中文本 */
  async function copyPath(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      toast.success('路径已复制')
      return true
    } catch {
      toast.info('浏览器拒绝了剪贴板写入：路径已在下方输入框选中，按 Ctrl+C 复制')
      return false
    }
  }

  function cancelInflight() {
    if (inflightId) tasks.cancel(inflightId)
  }

  function reset() {
    result.value = null
    validated.value = null
    validateError.value = ''
    runError.value = ''
  }

  return {
    formats, formatsLoading, formatsError, defaultOutDir, chosen, params, split, splitSeed, scripts,
    running, runError, result, validating, validateError, validated,
    reports, reportsLoading, reportsError,
    currentFormat, schema, splitKey, hasSplit, seedKey, splitSum, splitValid, splitError,
    payload, splitOutcome, canExport, exportWhy, validateDir, canValidate,
    check, checkIssues, checkIssueCount, scriptPaths, scriptNotes,
    loadFormats, selectFormat, setSplit, setSeed, setParam, setParamText,
    run, validate, loadReports, copyPath, cancelInflight, reset,
    folds: FOLD_NAMES,
  }
})

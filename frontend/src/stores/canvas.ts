/**
 * canvas store（UI规格 §5.2 标注画布）：单条目的草稿标注、撤销/重做栈、
 * 帧参数、绘制模式与选中态，以及保存/提交/审核/智能标注的角色动作。
 *
 * 坐标口径（BUG-008 更正）：box/poly/point 用 **native 图像像素空间**（Ann 模型即
 * 契约 §5.5 的"图像像素坐标"，源数组尺寸，不是含 letterbox 的出图画布尺寸）；
 * 出图画布与 native 的换算只走 meta.info.fit，见 contentRect 与 toCanvasX/toNativeX 系列。
 * interval/level 用 UTC 秒（t0,t1）。Ann 模型约束（model.py）在前端同序校验，
 * 保证提交前就是合法几何。所有本地编辑入撤销栈，保存是"整屏覆盖式提交"
 * （POST /api/annotations replace=true），非增量。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import * as api from '@/api/endpoints'
import type { AnnRow } from '@/api/types'
import { useCtxStore } from './ctx'
import { useToast } from './toast'

export type Geom = 'box' | 'poly' | 'point' | 'interval' | 'level'
export type Mode = 'select' | Geom
export type AnnState = 'draft' | 'submitted' | 'approved' | 'rejected'

/** 一条草稿标注：localId 供画布/列表稳定引用；id 为后端标注 id（保存后回填）。 */
export interface DraftAnn {
  localId: number
  id?: string
  geom: Geom
  coords: number[]
  label: string
  source: string
  state: AnnState
  confidence: number
  level: number
  slice: number | null
  note: string
  color: number[] | null
}

export interface FitRect {
  x0: number
  y0: number
  w: number
  h: number
  scale: number
}

export interface FrameParams {
  index?: number
  channel?: number
  cursor?: number
  stretch?: string
  colormap?: string
  gamma?: number
  [key: string]: unknown
}

const HIST_CAP = 200
const GEOM_COORDS: Record<Geom, number> = {
  box: 4, point: 2, interval: 2, level: 2, poly: 0,
}

/** 提交前合法性校验，与后端 Ann._check_coords 同序。 */
export function annValid(a: DraftAnn): string {
  if (!(a.geom in GEOM_COORDS)) return `未知几何 ${a.geom}`
  if (a.geom === 'poly') {
    if (a.coords.length < 6 || a.coords.length % 2) return 'poly 需 ≥3 顶点'
  } else if (a.coords.length !== GEOM_COORDS[a.geom]) {
    return `${a.geom} 需 ${GEOM_COORDS[a.geom]} 坐标`
  }
  if (a.coords.some((v) => !Number.isFinite(v))) return '坐标必须有限实数'
  if (a.geom === 'box' && (a.coords[2] < a.coords[0] || a.coords[3] < a.coords[1]))
    return 'box 右下须大于左上'
  if ((a.geom === 'interval' || a.geom === 'level') && a.coords[1] < a.coords[0])
    return '区间终点早于起点'
  return ''
}

function rowToDraft(row: AnnRow, localId: number): DraftAnn {
  const ann = row.ann
  return {
    localId,
    id: row.id,
    geom: ann.geom as Geom,
    coords: [...(ann.coords || [])],
    label: ann.label || '',
    source: ann.source || 'manual',
    state: (row.state as AnnState) || 'draft',
    confidence: Number(ann.confidence ?? 1),
    level: Number(ann.level ?? 0),
    slice: (ann.slice as number | null | undefined) ?? null,
    note: String(ann.note ?? ''),
    color: Array.isArray(ann.color) ? (ann.color as number[]) : null,
  }
}

export const useCanvasStore = defineStore('canvas', () => {
  const ctx = useCtxStore()

  // ---- 当前条目与视图 ----
  const currentUri = ref('')
  const modality = ref('')
  const frameW = ref(960)
  const frameH = ref(540)
  /**
   * fit 映射（BUG-008 / UI规格 v1.4 §5.2 坐标系条文）：`GET /api/frame/meta` 的
   * `info.fit = [x0, y0, w, h, scale]` 给出 native 图像（= Ann 坐标空间）等比贴进
   * 画布（frameW×frameH，含 letterbox）后的落位。Ann.coords 一律是 native 图像像素，
   * 因此叠加层必须走同一条 fit 映射，绘制落点必须做逆变换——否则人工标注存画布 px、
   * 机器标注存 native px，同一字段两套坐标系。series/event 出图即画布，无 fit → 恒等。
   */
  const fit = ref<FitRect | null>(null)
  /** series 出图的绘图区（画布像素）：时间→x 用它，轴外留白不参与映射 */
  const plotBox = ref<number[] | null>(null)
  /** Ann 坐标空间尺寸：有 fit 时 = 内容尺寸/scale，无 fit 时 = 画布尺寸 */
  const srcW = computed(() => (fit.value && fit.value.scale > 0 ? fit.value.w / fit.value.scale : frameW.value))
  const srcH = computed(() => (fit.value && fit.value.scale > 0 ? fit.value.h / fit.value.scale : frameH.value))
  const tRef = ref<number | null>(null)
  const tEnd = ref<number | null>(null)
  const channels = ref<string[]>([])
  const nViews = ref(0)
  const frameParams = ref<FrameParams>({ stretch: 'linear', gamma: 1.0, colormap: 'gray', cursor: 0, index: 0, channel: 0 })
  const zoom = ref(1)
  const loadingFrame = ref(false)

  // ---- 草稿与编辑态 ----
  const draftAnns = ref<DraftAnn[]>([])
  const mode = ref<Mode>('select')
  const label = ref('')
  const selId = ref<number | null>(null)
  const dirty = ref(false)
  const loading = ref(false)
  const saving = ref(false)
  let nextLocalId = 1

  // ---- 撤销栈（本地快照，深度 ≥100）----
  const hist = ref<string[]>([])
  const hp = ref(-1)

  const selected = computed(() => draftAnns.value.find((a) => a.localId === selId.value) || null)
  const isTimeModality = computed(() =>
    modality.value === 'series' || modality.value === 'event' || modality.value === 'video')
  /** 时序态的时间跨度（UTC 秒），供 interval/level 与时间轴映射 */
  const timeSpan = computed(() => {
    const a = tRef.value ?? 0
    const b = tEnd.value ?? 0
    return { t0: a, t1: b > a ? b : a + 1 }
  })

  /** 内容区在画布像素中的落位（letterbox 之后的有效绘图区） */
  const contentRect = computed(() => {
    const f = fit.value
    if (!f || !(f.scale > 0) || !(f.w > 0) || !(f.h > 0))
      return { x: 0, y: 0, w: frameW.value, h: frameH.value }
    return { x: f.x0, y: f.y0, w: f.w, h: f.h }
  })

  // ---- native 图像像素 ↔ 画布像素（BUG-008 的唯一换算口）----
  const toCanvasX = (nx: number): number => contentRect.value.x + nx * (fit.value?.scale ?? 1)
  const toCanvasY = (ny: number): number => contentRect.value.y + ny * (fit.value?.scale ?? 1)
  const toNativeX = (cx: number): number => (cx - contentRect.value.x) / (fit.value?.scale ?? 1)
  const toNativeY = (cy: number): number => (cy - contentRect.value.y) / (fit.value?.scale ?? 1)

  // ---- 同类别同色（UI规格 v1.4 §5.2）----
  // 取色优先级：ann.color（后端显式）> labels 表 color > 按标签名稳定散列入 PALETTE。
  // 与后端 render/overlay.py 的 PALETTE 同序，保证前后端同色。
  const PALETTE = ['rgb(255,80,80)', 'rgb(80,200,255)', 'rgb(255,200,60)', 'rgb(120,255,120)', 'rgb(220,120,255)']
  const labelColors = ref<Record<string, string>>({})
  function setLabelColors(labels: { name: string; color?: string }[]): void {
    const map: Record<string, string> = {}
    labels.forEach((l, i) => { map[l.name] = l.color && l.color.trim() ? l.color.trim() : PALETTE[i % PALETTE.length] })
    labelColors.value = map
  }
  function colorOf(ann: DraftAnn): string {
    // 1) 后端显式 color（rgb 三元组）优先
    if (Array.isArray(ann.color) && ann.color.length >= 3)
      return `rgb(${ann.color.slice(0, 3).map((v) => Math.round(v)).join(',')})`
    // 未标类别走中性灰：不占用 PALETTE，免得空 label 与某个类别撞色
    if (!ann.label) return 'rgb(180,184,196)'
    // 2) labels 表 color；3) 标签名稳定散列——同一 label 恒得同一色，几何/来源都不参与取色
    const lc = labelColors.value[ann.label]
    if (lc) return lc
    {
      let h = 0
      for (const ch of ann.label) h = (h * 31 + ch.charCodeAt(0)) >>> 0
      return PALETTE[h % PALETTE.length]
    }
    return PALETTE[0]
  }

  function snapshot(): string {
    return JSON.stringify(draftAnns.value.map((a) => ({
      localId: a.localId, id: a.id, geom: a.geom, coords: a.coords, label: a.label,
      source: a.source, state: a.state, confidence: a.confidence, level: a.level,
      slice: a.slice, note: a.note, color: a.color,
    })))
  }

  /** 每次结构性编辑后调用：截断 redo 分支、压入快照、置 dirty。 */
  function pushHist(): void {
    const snap = snapshot()
    if (hist.value.length && hist.value[hp.value] === snap) return
    hist.value = hist.value.slice(0, hp.value + 1)
    hist.value.push(snap)
    if (hist.value.length > HIST_CAP) hist.value.shift()
    hp.value = hist.value.length - 1
    dirty.value = true
  }

  function restore(snap: string): void {
    const parsed = JSON.parse(snap) as DraftAnn[]
    draftAnns.value = parsed
    nextLocalId = Math.max(0, ...parsed.map((a) => a.localId)) + 1
    selId.value = null
  }

  function undo(): boolean {
    if (hp.value > 0) {
      hp.value -= 1
      restore(hist.value[hp.value])
      dirty.value = true
      return true
    }
    return false
  }

  function redo(): boolean {
    if (hp.value < hist.value.length - 1) {
      hp.value += 1
      restore(hist.value[hp.value])
      dirty.value = true
      return true
    }
    return false
  }

  const canUndo = computed(() => hp.value > 0)
  const canRedo = computed(() => hp.value < hist.value.length - 1)

  // ---- 几何操作 ----
  function addAnn(partial: Partial<DraftAnn> & { geom: Geom; coords: number[] }): DraftAnn | null {
    const a: DraftAnn = {
      localId: nextLocalId++,
      geom: partial.geom,
      coords: [...partial.coords],
      label: partial.label ?? label.value,
      source: partial.source ?? 'manual',
      state: (partial.state as AnnState) ?? 'draft',
      confidence: partial.confidence ?? 1,
      level: partial.level ?? 0,
      slice: partial.slice ?? null,
      note: partial.note ?? '',
      color: partial.color ?? null,
    }
    if (annValid(a)) return null
    draftAnns.value.push(a)
    selId.value = a.localId
    pushHist()
    return a
  }

  /** 就地改坐标/属性（拖拽、缩放、改类）；调用方负责节流，落定后 endEdit 入栈。 */
  function mutate(localId: number, patch: Partial<DraftAnn>): void {
    const a = draftAnns.value.find((x) => x.localId === localId)
    if (!a) return
    Object.assign(a, patch)
  }

  /**
   * 一次结构性编辑落定（拖拽/缩放/顶点/改类）→ 入栈一次。
   * 有效性只在 save 前拦截，不作为入栈条件：非法中间态也必须能被 Ctrl+Z 撤掉，
   * 否则一次拖坏几何就再也退不回去（同内容重复快照由 pushHist 自身去重）。
   */
  function endEdit(): void {
    if (selected.value) pushHist()
  }

  function deleteAnn(localId: number): void {
    draftAnns.value = draftAnns.value.filter((a) => a.localId !== localId)
    if (selId.value === localId) selId.value = null
    pushHist()
  }

  function select(localId: number | null): void {
    selId.value = localId
  }

  function setMode(m: Mode): void {
    mode.value = m
    if (m !== 'select') selId.value = null
  }

  // ---- 载入/保存 ----
  async function openItem(uri: string): Promise<void> {
    if (!ctx.version) return
    loading.value = true
    try {
      currentUri.value = uri
      const [resp, meta] = await Promise.all([
        api.getAnnotations(ctx.version, uri),
        api.getFrameMeta(uri, { width: frameW.value, height: frameH.value }),
      ])
      modality.value = String(meta.modality || '')
      if (meta.width) frameW.value = Number(meta.width)
      if (meta.height) frameH.value = Number(meta.height)
      const info = (meta.info || {}) as Record<string, unknown>
      const f = info.fit
      fit.value = Array.isArray(f) && f.length >= 5 && Number(f[4]) > 0
        ? { x0: Number(f[0]), y0: Number(f[1]), w: Number(f[2]), h: Number(f[3]), scale: Number(f[4]) }
        : null
      const pb = info.plot_box
      plotBox.value = Array.isArray(pb) && pb.length >= 4 ? pb.map(Number) : null
      tRef.value = (meta.t_ref as number | null) ?? null
      tEnd.value = (meta.t_end as number | null) ?? null
      channels.value = (meta.channels as string[]) || []
      nViews.value = Number(meta.n_views || 0)
      nextLocalId = 1
      draftAnns.value = resp.annotations.map((r) => rowToDraft(r, nextLocalId++))
      // cursor 与 t_ref 同为 UTC 秒：起始光标必须落在时间轴起点，否则 0 会显示成 1970 年
      frameParams.value = {
        stretch: 'linear', gamma: 1.0, colormap: 'gray',
        cursor: Number.isFinite(tRef.value as number) ? (tRef.value as number) : 0,
        index: 0, channel: 0,
      }
      hist.value = [snapshot()]
      hp.value = 0
      dirty.value = false
      selId.value = null
    } finally {
      loading.value = false
    }
  }

  async function save(): Promise<boolean> {
    if (!ctx.version || !currentUri.value) return false
    const bad = draftAnns.value.find((a) => annValid(a))
    if (bad) {
      useToast().danger(`几何非法：#${bad.localId} ${annValid(bad)}`)
      return false
    }
    saving.value = true
    try {
      const payload = draftAnns.value.map((a) => ({
        geom: a.geom, coords: a.coords, label: a.label, source: a.source,
        confidence: a.confidence, level: a.level, slice: a.slice, note: a.note,
        color: a.color,
      }))
      const resp = await api.putAnnotations(ctx.version, currentUri.value, payload, ctx.role, 'draft', true)
      // 回填后端 id：按提交顺序对应（replace=true 全删重写）
      const { annotations } = await api.getAnnotations(resp.version_id, currentUri.value)
      const bySig = new Map<string, AnnRow>()
      for (const r of annotations) bySig.set(`${r.ann.geom}|${(r.ann.coords || []).join(',')}|${r.ann.label}`, r)
      for (const a of draftAnns.value) {
        const hit = bySig.get(`${a.geom}|${a.coords.join(',')}|${a.label}`)
        if (hit) { a.id = hit.id; a.state = (hit.state as AnnState) || a.state }
      }
      hist.value = [snapshot()]
      hp.value = 0
      dirty.value = false
      useToast().success('已保存（整屏覆盖式提交，草稿态）')
      return true
    } catch (err) {
      useToast().danger(`保存失败：${err instanceof Error ? err.message : String(err)}`)
      return false
    } finally {
      saving.value = false
    }
  }

  async function submitItem(): Promise<void> {
    if (!ctx.version || !currentUri.value) return
    if (dirty.value) {
      const ok = await save()
      if (!ok) return
    }
    try {
      const resp = await api.submitItem(ctx.version, currentUri.value, ctx.role)
      useToast().success(`已提交审核（${resp.n_submitted} 条 draft→submitted）`)
      await openItem(currentUri.value)
    } catch (err) {
      useToast().danger(`提交失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  /** 单条状态迁移（审核区逐条通过/驳回）。 */
  async function reviewOne(a: DraftAnn, action: 'approve' | 'reject', reason = ''): Promise<void> {
    if (!a.id) { useToast().warning('该条尚未保存，无法审核'); return }
    try {
      const resp = await api.setAnnState(a.id, ctx.role, action, reason)
      a.state = resp.state as AnnState
      pushHist()
    } catch (err) {
      useToast().danger(`审核失败（403 越权？）：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  /** 批量验收/驳回：显式传 action（契约 §5.5 ※6）；n=0 视为无生效操作（DEV-03）。 */
  async function reviewBulk(action: 'approve' | 'reject', maxConfidence?: number | null): Promise<void> {
    if (!ctx.version) return
    try {
      const resp = await api.reviewBulk(ctx.version, action, ctx.role, '', maxConfidence ?? null)
      if (resp.n === 0) useToast().info('批量操作无生效条目（n=0）')
      else useToast().success(`批量${action === 'approve' ? '验收' : '驳回'} ${resp.n} 条`)
      if (currentUri.value) await openItem(currentUri.value)
    } catch (err) {
      useToast().danger(`批量失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  // ---- 智能标注：apply→undo 参数链（契约 §7）----
  const smartAppliedVersion = ref('')
  const smartBusy = ref(false)

  async function smartApply(params: Record<string, unknown>): Promise<boolean> {
    if (!ctx.version) return false
    smartBusy.value = true
    try {
      const resp = await api.smartApply(ctx.version, [currentUri.value], params)
      const newVid = String(resp.version_id || '')
      if (newVid) {
        smartAppliedVersion.value = newVid
        useToast().success(`已落新版本（人工标注已保留并 carry）；撤销按钮绑定 version_id=${newVid.slice(0, 8)}…`)
        // 切到新版本刷新：ctx.setVersion 触发整体重载
        ctx.setVersion(newVid)
        await openItem(currentUri.value)
        return true
      }
      useToast().warning('apply 未返回新 version_id')
      return false
    } catch (err) {
      useToast().danger(`预标注落库失败：${err instanceof Error ? err.message : String(err)}`)
      return false
    } finally {
      smartBusy.value = false
    }
  }

  async function smartUndo(): Promise<void> {
    if (!smartAppliedVersion.value) return
    smartBusy.value = true
    try {
      await api.smartUndo(smartAppliedVersion.value, ctx.role)
      useToast().info('已回退到父版本（不删除任何数据）')
      const idx = ctx.versions.findIndex((v) => v.id === smartAppliedVersion.value)
      const parent = idx > 0 ? ctx.versions[idx - 1].id : (ctx.head || '')
      smartAppliedVersion.value = ''
      if (parent) ctx.setVersion(parent)
      await openItem(currentUri.value)
    } catch (err) {
      useToast().danger(`撤销机器结果失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      smartBusy.value = false
    }
  }

  // ---- 帧参数 ----
  function setParam(key: keyof FrameParams, value: unknown): void {
    frameParams.value = { ...frameParams.value, [key]: value }
  }

  function reset(): void {
    currentUri.value = ''
    modality.value = ''
    fit.value = null
    plotBox.value = null
    draftAnns.value = []
    frameParams.value = { stretch: 'linear', gamma: 1.0, colormap: 'gray', cursor: 0, index: 0, channel: 0 }
    hist.value = []
    hp.value = -1
    dirty.value = false
    selId.value = null
    mode.value = 'select'
    zoom.value = 1
    smartAppliedVersion.value = ''
  }

  return {
    currentUri, modality, frameW, frameH, frameParams, zoom, loadingFrame,
    fit, plotBox, srcW, srcH, contentRect, toCanvasX, toCanvasY, toNativeX, toNativeY,
    tRef, tEnd, channels, nViews, isTimeModality, timeSpan,
    labelColors, setLabelColors, colorOf,
    draftAnns, mode, label, selId, dirty, loading, saving, selected,
    canUndo, canRedo, smartAppliedVersion, smartBusy,
    openItem, save, submitItem, reviewOne, reviewBulk, smartApply, smartUndo,
    addAnn, mutate, endEdit, deleteAnn, select, setMode, undo, redo, pushHist,
    setParam, reset,
  }
})

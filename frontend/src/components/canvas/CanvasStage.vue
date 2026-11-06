<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { frameUrl } from '@/api/endpoints'
import { useCanvasStore, type DraftAnn, type Geom } from '@/stores/canvas'
import { useToast } from '@/stores/toast'

const store = useCanvasStore()
const toast = useToast()

const svgEl = ref<SVGSVGElement | null>(null)
const imgEl = ref<HTMLImageElement | null>(null)

// ---- 帧图 src：参数变化 120ms 节流 + 取消在途（§8）----
const displaySrc = ref('')
let throttleTimer: ReturnType<typeof setTimeout> | null = null
function buildFrameUrl(): string {
  const p: Record<string, unknown> = {
    width: store.frameW, height: store.frameH,
    stretch: store.frameParams.stretch, gamma: store.frameParams.gamma,
    colormap: store.frameParams.colormap,
  }
  if (store.isTimeModality) p.cursor = store.frameParams.cursor ?? store.timeSpan.t0
  // cube 只有切片轴（index），没有通道概念：多发一个 channel 会让后端
  // render_array 直接 TypeError（500 帧出不来），所以这里按模态严格给键。
  if (store.modality === 'cube') p.index = store.frameParams.index ?? 0
  if (store.modality === 'series') p.channel = store.frameParams.channel ?? 0
  return frameUrl(store.currentUri, p)
}
function scheduleFrame(): void {
  if (!store.currentUri) { displaySrc.value = ''; return }
  store.loadingFrame = true
  if (throttleTimer) clearTimeout(throttleTimer)
  throttleTimer = setTimeout(() => {
    throttleTimer = null
    displaySrc.value = buildFrameUrl()
  }, 120)
}
watch(() => [store.currentUri, store.frameParams, store.frameW, store.frameH], scheduleFrame, { deep: true })
function onImgLoad(): void { store.loadingFrame = false }
function onImgError(): void { store.loadingFrame = false; toast.warning('帧渲染失败，可调整参数或稍后重试') }

// ---- fit 映射（BUG-008）：Ann 坐标 = native 图像像素，出图画布含 letterbox ----
// 叠加层与帧 <img> 共用这一条换算；越界几何照常绘制、不裁剪不改写（UI规格 v1.4 §5.2）。
const fs = computed(() => (store.fit && store.fit.scale > 0 ? store.fit.scale : 1))
const fox = computed(() => (store.fit ? store.fit.x0 : 0))
const foy = computed(() => (store.fit ? store.fit.y0 : 0))
const X = (n: number): number => fox.value + n * fs.value
const Y = (n: number): number => foy.value + n * fs.value
const L = (n: number): number => n * fs.value
/** 画布上的点标半径：屏幕像素恒定，不随 fit/zoom 变形 */
const marker = (px: number): number => px / store.zoom

// ---- 坐标采集：屏幕 → 画布 px → native 图像 px ----
function toCanvas(evt: MouseEvent): { x: number; y: number } {
  const svg = svgEl.value!
  const pt = svg.createSVGPoint()
  pt.x = evt.clientX; pt.y = evt.clientY
  const m = svg.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, y: p.y }
}
/** 落笔唯一入口：画布 px 逆 fit 变换成 native，存进 Ann.coords 的一律是图像像素 */
function toNative(evt: MouseEvent): { x: number; y: number } {
  const c = toCanvas(evt)
  return { x: (c.x - fox.value) / fs.value, y: (c.y - foy.value) / fs.value }
}

// ---- 时间 ↔ 画布 x：series 用 plot_box，其余用 fit 内容区（letterbox 不参与映射）----
function bandRange(): [number, number] {
  const pb = store.plotBox
  if (pb && pb.length >= 4) return [pb[0], pb[2]]
  const r = store.contentRect
  return [r.x, r.x + r.w]
}
function xToTime(x: number): number {
  const { t0, t1 } = store.timeSpan
  const [bx0, bx1] = bandRange()
  const frac = Math.max(0, Math.min(1, (x - bx0) / (bx1 - bx0 || 1)))
  return t0 + frac * (t1 - t0)
}
function timeToX(t: number): number {
  const { t0, t1 } = store.timeSpan
  const [bx0, bx1] = bandRange()
  return bx0 + ((t - t0) / (t1 - t0 || 1)) * (bx1 - bx0)
}

// ---- 绘制草稿态（native 像素）----
type Draft =
  | { kind: 'box'; x1: number; y1: number; x2: number; y2: number }
  | { kind: 'interval'; a: number; b: number }
  | { kind: 'level'; a: number; b: number }
  | null
const draft = ref<Draft>(null)
const polyBuf = ref<{ x: number; y: number }[]>([])

// ---- 拖拽移动 / 缩放 / 顶点 ----
const dragLocalId = ref<number | null>(null)
let dragHandle = -1 // box: 0-7 控制点, -1=整体; poly: 顶点 index
let dragStart = { x: 0, y: 0 }
let dragStartCn = { x: 0, y: 0 }
let dragOrig: number[] = []

// poly 顶点编辑会话（v1.4 句柄验收③：双击进入，Enter 提交、Esc 回退本次编辑）
const vertexEditId = ref<number | null>(null)
let vertexEntry: number[] = []

// 拖拽实时读数（v1.4：拖拽中跟随光标的 mono 坐标读数）
const readout = ref('')
const readPos = ref({ x: 0, y: 0 })

// 句柄尺寸按屏幕像素恒定：视觉 8px 见方、命中层 12px 见方，不随 zoom 缩放（v1.4 rev1）
const hitHalf = computed(() => 6 / store.zoom)
const visHalf = computed(() => 4 / store.zoom)

function geomAllowed(g: Geom): boolean {
  if (g === 'interval' || g === 'level') return store.isTimeModality
  return true
}

// 手势期把 move/up 绑到 window：指针离开画布、或任何被打断的拖拽会话都不会让 mouseup 丢失
// （BUG-011 的 is-drag 残留属同一类：mouseup 没派发，onUp 就不会跑）
let gestureBound = false
function bindGesture(): void {
  if (gestureBound) return
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  gestureBound = true
}
function unbindGesture(): void {
  if (!gestureBound) return
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseup', onUp)
  gestureBound = false
}
/** 手势作废：清拖拽/草稿态并解绑，保证 is-drag、读数不残留 */
function resetGesture(): void {
  dragLocalId.value = null; dragHandle = -1; draft.value = null
  readout.value = ''
  hoverHandle.value = null
  unbindGesture()
}

/** 句柄胜出者（UI规格 v1.4 rev4）：低 zoom 下相邻 12px 命中层会重叠，
 *  命中不再交给 DOM 顺序，而是取屏幕像素距指针最近的那个；hover 高亮与 mousedown
 *  用同一个函数，保证"高亮哪条边就拖哪条边"。 */
interface HandlePick { id: number; handle: number; dist: number }
const hoverHandle = ref<{ id: number; handle: number } | null>(null)

function pickHandle(evt: MouseEvent): HandlePick | null {
  const svg = svgEl.value
  if (!svg) return null
  let best: HandlePick | null = null
  for (const h of Array.from(svg.querySelectorAll('g.handle'))) {
    const gid = Number(h.closest('g.ann')?.getAttribute('data-id'))
    const a = store.draftAnns.find((x) => x.localId === gid)
    if (!a || a.state === 'approved') continue
    const b = h.getBoundingClientRect()
    if (evt.clientX < b.left || evt.clientX > b.right || evt.clientY < b.top || evt.clientY > b.bottom) continue
    const dist = Math.hypot(evt.clientX - (b.left + b.width / 2), evt.clientY - (b.top + b.height / 2))
    if (!best || dist < best.dist) best = { id: gid, handle: Number(h.getAttribute('data-handle')), dist }
  }
  return best
}
const isHot = (id: number, handle: number): boolean =>
  hoverHandle.value?.id === id && hoverHandle.value?.handle === handle
/** 胜出句柄视觉层放大到 ~10px（命中层维持 12px 不随 zoom 变，rev1 口径不动） */
const hotHalf = computed(() => 5 / store.zoom)

function onDown(evt: MouseEvent): void {
  if (!store.currentUri) return
  // BUG-011：Blink 在 mousedown 后越过拖拽阈值会对 SVG 内容起原生拖拽，dragstart 之后
  // 页面再也收不到 mousemove/mouseup（顶点只跟第一段、is-drag 卡住）。mousedown 一律
  // preventDefault 让拖拽会话根本起不来，手势期的 move/up 再绑到 window 保证必达。
  evt.preventDefault()
  const cn = toCanvas(evt)
  const p = toNative(evt)
  if (store.mode === 'select') {
    const hit = pickHandle(evt)
    if (hit) {
      const a = store.draftAnns.find((x) => x.localId === hit.id)
      if (!a || a.state === 'approved') return
      dragLocalId.value = hit.id; dragHandle = hit.handle
      dragStart = p; dragStartCn = cn; dragOrig = [...a.coords]
      if (store.selId !== hit.id) store.select(hit.id)
      hoverHandle.value = null
      bindGesture()
      return
    }
    const g = (evt.target as Element).closest('g.ann')
    if (g) {
      const id = Number(g.getAttribute('data-id'))
      if (id !== vertexEditId.value) leaveVertexSession(true)
      store.select(id)
      const a = store.draftAnns.find((x) => x.localId === id)
      if (a && a.state !== 'approved') {
        dragLocalId.value = id; dragHandle = -1; dragStart = p; dragStartCn = cn; dragOrig = [...a.coords]
        bindGesture()
      }
      return
    }
    leaveVertexSession(true)
    store.select(null)
    return
  }
  leaveVertexSession(true)
  if (store.mode === 'point') {
    store.addAnn({ geom: 'point', coords: [Math.round(p.x), Math.round(p.y)] })
    return
  }
  if (store.mode === 'poly') {
    if (!geomAllowed('poly')) return
    polyBuf.value.push(p)
    return
  }
  if (store.mode === 'box') { draft.value = { kind: 'box', x1: p.x, y1: p.y, x2: p.x, y2: p.y }; bindGesture(); return }
  if (store.mode === 'interval') {
    if (!geomAllowed('interval')) { toast.warning('时段仅时序态可用（Ann 模型约束）'); return }
    draft.value = { kind: 'interval', a: xToTime(cn.x), b: xToTime(cn.x) }; bindGesture(); return
  }
  if (store.mode === 'level') {
    if (!geomAllowed('level')) { toast.warning('水平带仅时序态可用（Ann 模型约束）'); return }
    draft.value = { kind: 'level', a: xToTime(cn.x), b: xToTime(cn.x) }; bindGesture(); return
  }
}

function onMove(evt: MouseEvent): void {
  const cn = toCanvas(evt)
  const p = toNative(evt)
  if (draft.value) {
    if (draft.value.kind === 'box') { draft.value.x2 = p.x; draft.value.y2 = p.y }
    else if (draft.value.kind === 'interval' || draft.value.kind === 'level') draft.value.b = xToTime(cn.x)
    return
  }
  if (dragLocalId.value != null) {
    const a = store.draftAnns.find((x) => x.localId === dragLocalId.value)
    if (!a) return
    const dx = p.x - dragStart.x, dy = p.y - dragStart.y
    const c = dragOrig
    if (a.geom === 'box' && dragHandle >= 0) {
      // 控制点：0 tl 1 t 2 tr 3 r 4 br 5 b 6 bl 7 l —— 各自移动对应边
      let [x0, y0, x1, y1] = c
      if ([0, 6, 7].includes(dragHandle)) x0 = c[0] + dx
      if ([2, 3, 4].includes(dragHandle)) x1 = c[2] + dx
      if ([0, 1, 2].includes(dragHandle)) y0 = c[1] + dy
      if ([4, 5, 6].includes(dragHandle)) y1 = c[3] + dy
      store.mutate(a.localId, { coords: normBox([x0, y0, x1, y1]) })
    } else if (a.geom === 'poly' && dragHandle >= 0) {
      const next = [...c]
      next[dragHandle * 2] = Math.round(c[dragHandle * 2] + dx)
      next[dragHandle * 2 + 1] = Math.round(c[dragHandle * 2 + 1] + dy)
      store.mutate(a.localId, { coords: next })
    } else if (a.geom === 'box') {
      store.mutate(a.localId, { coords: [c[0] + dx, c[1] + dy, c[2] + dx, c[3] + dy].map((v) => Math.round(v)) })
    } else if (a.geom === 'point') {
      store.mutate(a.localId, { coords: [Math.round(c[0] + dx), Math.round(c[1] + dy)] })
    } else if (a.geom === 'poly') {
      const next: number[] = []
      for (let i = 0; i < c.length; i += 2) next.push(Math.round(c[i] + dx), Math.round(c[i + 1] + dy))
      store.mutate(a.localId, { coords: next })
    } else if (a.geom === 'interval' || a.geom === 'level') {
      const [bx0, bx1] = bandRange()
      const d = (cn.x - dragStartCn.x) / (bx1 - bx0 || 1) * (store.timeSpan.t1 - store.timeSpan.t0)
      store.mutate(a.localId, { coords: [c[0] + d, c[1] + d] })
    }
    readPos.value = cn
    readout.value = readText(a, p)
    return
  }
}

/** 悬停：胜出句柄即时高亮（rev4「拖错可即时察觉」）+ 移动光标 */
function onHover(evt: MouseEvent): void {
  if (dragLocalId.value != null || draft.value) return
  if (store.mode !== 'select' || !svgEl.value) return
  const hit = pickHandle(evt)
  hoverHandle.value = hit ? { id: hit.id, handle: hit.handle } : null
  if (!hit) {
    const g = (evt.target as Element).closest('g.ann')
    svgEl.value.style.cursor = g ? 'move' : 'default'
  }
}
function onHoverLeave(): void { hoverHandle.value = null }

/** 拖拽中的实时坐标读数（native 图像像素，mono + tabular-nums） */
function readText(a: DraftAnn, p: { x: number; y: number }): string {
  if (a.geom === 'box') {
    return `${Math.round(a.coords[2] - a.coords[0])}×${Math.round(a.coords[3] - a.coords[1])} @ ${Math.round(a.coords[0])},${Math.round(a.coords[1])}`
  }
  if (a.geom === 'point') return `${Math.round(a.coords[0])}, ${Math.round(a.coords[1])}`
  if (a.geom === 'poly' && dragHandle >= 0) {
    return `${Math.round(a.coords[dragHandle * 2])}, ${Math.round(a.coords[dragHandle * 2 + 1])}`
  }
  if (a.geom === 'interval' || a.geom === 'level') return `${(a.coords[1] - a.coords[0]).toFixed(2)} s`
  return `${Math.round(p.x)}, ${Math.round(p.y)}`
}

function normBox(c: number[]): number[] {
  return [Math.round(Math.min(c[0], c[2])), Math.round(Math.min(c[1], c[3])),
    Math.round(Math.max(c[0], c[2])), Math.round(Math.max(c[1], c[3]))]
}

function onUp(): void {
  if (draft.value) {
    const d = draft.value
    if (d.kind === 'box' && Math.abs(d.x2 - d.x1) > 4 && Math.abs(d.y2 - d.y1) > 4) {
      store.addAnn({ geom: 'box', coords: normBox([d.x1, d.y1, d.x2, d.y2]) })
    } else if (d.kind === 'interval' && Math.abs(d.b - d.a) > 0.01) {
      store.addAnn({ geom: 'interval', coords: [Math.min(d.a, d.b), Math.max(d.a, d.b)] })
    } else if (d.kind === 'level' && Math.abs(d.b - d.a) > 0.01) {
      store.addAnn({ geom: 'level', coords: [Math.min(d.a, d.b), Math.max(d.a, d.b)], level: 1 })
    }
    draft.value = null
  }
  const id = dragLocalId.value
  if (id != null) {
    const a = store.draftAnns.find((x) => x.localId === id)
    const inVertexSession = vertexEditId.value != null && vertexEditId.value === id && dragHandle >= 0
    if (a && a.geom === 'box' && dragHandle >= 0 &&
      (Math.abs(a.coords[2] - a.coords[0]) < 4 || Math.abs(a.coords[3] - a.coords[1]) < 4)) {
      // 退化保护（v1.4）：缩放到宽或高 <4px 视为退化，回弹原尺寸、不入撤销栈
      store.mutate(a.localId, { coords: [...dragOrig] })
    } else if (a && !inVertexSession) {
      store.endEdit()
    }
  }
  dragLocalId.value = null; dragHandle = -1
  readout.value = ''
  unbindGesture()
}

function closePoly(): void {
  if (polyBuf.value.length >= 3) {
    const c: number[] = []
    polyBuf.value.forEach((q) => c.push(Math.round(q.x), Math.round(q.y)))
    store.addAnn({ geom: 'poly', coords: c })
  }
  polyBuf.value = []
}
function cancelPoly(): void { polyBuf.value = [] }

// ---- poly 顶点编辑会话：双击进入，Enter 提交一次撤销步，Esc 回退本次编辑 ----
function enterVertexEdit(id: number): void {
  const a = store.draftAnns.find((x) => x.localId === id)
  if (!a || a.geom !== 'poly' || a.state === 'approved') return
  vertexEditId.value = id
  vertexEntry = [...a.coords]
  store.select(id)
}
function leaveVertexSession(commit: boolean): boolean {
  const id = vertexEditId.value
  if (id == null) return false
  vertexEditId.value = null
  const a = store.draftAnns.find((x) => x.localId === id)
  if (!a) return true
  if (commit) {
    if (JSON.stringify(a.coords) !== JSON.stringify(vertexEntry)) store.endEdit()
  } else {
    store.mutate(id, { coords: [...vertexEntry] })
  }
  return true
}
function commitVertex(): boolean { return leaveVertexSession(true) }
function cancelVertexEdit(): boolean { return leaveVertexSession(false) }

function onDbl(evt: MouseEvent): void {
  if (store.mode === 'poly') { closePoly(); return }
  if (store.mode !== 'select') return
  const g = (evt.target as Element).closest('g.ann')
  if (g) { enterVertexEdit(Number(g.getAttribute('data-id'))); return }
  // 双击落在描边外（poly 只有 2px stroke，很难连续命中两次）：按当前选中的 poly 进入顶点编辑
  const sel = store.selected
  if (sel && sel.geom === 'poly') enterVertexEdit(sel.localId)
}

defineExpose({ closePoly, cancelPoly, commitVertex, cancelVertexEdit })

// ---- 渲染辅助 ----
// 来源只用实线(human)/虚线(machine)，状态只用右栏 Tag——几何颜色一律让位给类别（UI规格 v1.4）
function lineDash(a: DraftAnn): string {
  return a.source === 'manual' ? '' : '6 3'
}
const boxHandles = (c: number[]) => ([
  { h: 0, x: c[0], y: c[1] }, { h: 1, x: (c[0] + c[2]) / 2, y: c[1] }, { h: 2, x: c[2], y: c[1] },
  { h: 3, x: c[2], y: (c[1] + c[3]) / 2 }, { h: 4, x: c[2], y: c[3] }, { h: 5, x: (c[0] + c[2]) / 2, y: c[3] },
  { h: 6, x: c[0], y: c[3] }, { h: 7, x: c[0], y: (c[1] + c[3]) / 2 },
])

const cursorX = computed(() => {
  if (!store.isTimeModality) return 0
  return timeToX(store.frameParams.cursor ?? store.timeSpan.t0)
})

const stageStyle = computed(() => ({ transform: `scale(${store.zoom})`, transformOrigin: 'top left' }))
const viewBox = computed(() => `0 0 ${store.frameW} ${store.frameH}`)
const polyPoints = (c: number[]): string => {
  const out: string[] = []
  for (let i = 0; i < c.length; i += 2) out.push(`${X(c[i])},${Y(c[i + 1])}`)
  return out.join(' ')
}

watch(() => store.mode, (m) => { if (m !== 'select') leaveVertexSession(true); resetGesture() })
watch(() => store.currentUri, () => { vertexEditId.value = null; dragLocalId.value = null; resetGesture() })

onBeforeUnmount(() => { if (throttleTimer) clearTimeout(throttleTimer); unbindGesture() })
</script>

<template>
  <div class="stage-wrap" @dragstart.prevent>
    <div v-if="store.loadingFrame" class="stage-spinner" role="status" aria-label="帧加载中"><span /></div>
    <div class="stage" :style="stageStyle">
      <img
        v-if="displaySrc"
        ref="imgEl"
        :src="displaySrc"
        class="stage__frame"
        :width="store.frameW"
        :height="store.frameH"
        alt="数据帧"
        draggable="false"
        @load="onImgLoad"
        @error="onImgError"
      />
      <div v-else class="stage__empty" :style="{ width: store.frameW + 'px', height: store.frameH + 'px' }">
        无帧数据
      </div>
      <svg
        ref="svgEl"
        class="stage__svg"
        :viewBox="viewBox"
        :width="store.frameW"
        :height="store.frameH"
        @mousedown="onDown"
        @mousemove="onHover"
        @mouseleave="onHoverLeave"
        @dblclick="onDbl"
      >
        <!-- 已存标注：几何经 fit 正映射，coords 原值即 Ann 存储值（native 图像像素） -->
        <g
          v-for="a in store.draftAnns"
          :key="a.localId"
          class="ann"
          :data-id="a.localId"
          :class="{ 'is-sel': a.localId === store.selId, 'is-drag': a.localId === dragLocalId }"
        >
          <template v-if="a.geom === 'box'">
            <rect :x="X(Math.min(a.coords[0], a.coords[2]))" :y="Y(Math.min(a.coords[1], a.coords[3]))"
              :width="L(Math.abs(a.coords[2] - a.coords[0]))" :height="L(Math.abs(a.coords[3] - a.coords[1]))"
              :stroke="store.colorOf(a)" :stroke-dasharray="lineDash(a)" fill="none" stroke-width="2" />
            <text :x="X(Math.min(a.coords[0], a.coords[2])) + 3" :y="Y(Math.min(a.coords[1], a.coords[3])) - 4"
              class="ann__label" :fill="store.colorOf(a)">{{ a.label || a.geom }}</text>
            <template v-if="a.localId === store.selId && store.mode === 'select'">
              <g v-for="hd in boxHandles(a.coords)" :key="hd.h" :data-handle="hd.h" class="handle">
                <rect :x="X(hd.x) - hitHalf" :y="Y(hd.y) - hitHalf" :width="hitHalf * 2" :height="hitHalf * 2"
                  fill="transparent" />
                <rect
                  :x="X(hd.x) - (isHot(a.localId, hd.h) ? hotHalf : visHalf)"
                  :y="Y(hd.y) - (isHot(a.localId, hd.h) ? hotHalf : visHalf)"
                  :width="(isHot(a.localId, hd.h) ? hotHalf : visHalf) * 2"
                  :height="(isHot(a.localId, hd.h) ? hotHalf : visHalf) * 2"
                  class="handle__vis" :class="{ 'is-hot': isHot(a.localId, hd.h) }"
                />
              </g>
            </template>
          </template>
          <template v-else-if="a.geom === 'point'">
            <circle :cx="X(a.coords[0])" :cy="Y(a.coords[1])" :r="marker(6)" :stroke="store.colorOf(a)"
              :stroke-dasharray="lineDash(a)" fill="rgba(0,0,0,.3)" stroke-width="2" />
          </template>
          <template v-else-if="a.geom === 'poly'">
            <polygon :points="polyPoints(a.coords)" :stroke="store.colorOf(a)"
              :stroke-dasharray="lineDash(a)" fill="none" stroke-width="2" />
            <template v-if="vertexEditId === a.localId">
              <g v-for="(_, vi) in (a.coords.length / 2)" :key="vi" :data-handle="vi" class="handle">
                <circle :cx="X(a.coords[vi * 2])" :cy="Y(a.coords[vi * 2 + 1])" :r="hitHalf" fill="transparent" />
                <circle
                  :cx="X(a.coords[vi * 2])" :cy="Y(a.coords[vi * 2 + 1])"
                  :r="isHot(a.localId, vi) ? hotHalf : visHalf"
                  class="handle__vis" :class="{ 'is-hot': isHot(a.localId, vi) }"
                />
              </g>
            </template>
          </template>
          <template v-else-if="a.geom === 'interval'">
            <rect :x="timeToX(a.coords[0])" :y="store.contentRect.y"
              :width="Math.max(2, timeToX(a.coords[1]) - timeToX(a.coords[0]))" :height="store.contentRect.h"
              :stroke="store.colorOf(a)" :fill="store.colorOf(a)" fill-opacity="0.12"
              :stroke-dasharray="lineDash(a)" stroke-width="1.5" />
          </template>
          <template v-else-if="a.geom === 'level'">
            <rect :x="timeToX(a.coords[0])" :y="store.contentRect.y"
              :width="Math.max(2, timeToX(a.coords[1]) - timeToX(a.coords[0]))" :height="store.contentRect.h"
              :stroke="store.colorOf(a)" :fill="store.colorOf(a)" fill-opacity="0.14"
              :stroke-dasharray="lineDash(a)" stroke-width="1.5" />
          </template>
        </g>

        <!-- 绘制中草稿 -->
        <rect v-if="draft && draft.kind === 'box'" :x="X(Math.min(draft.x1, draft.x2))"
          :y="Y(Math.min(draft.y1, draft.y2))" :width="L(Math.abs(draft.x2 - draft.x1))"
          :height="L(Math.abs(draft.y2 - draft.y1))"
          class="draft" stroke="var(--accent)" stroke-dasharray="5 4" fill="none" stroke-width="2" />
        <polygon v-if="polyBuf.length" :points="polyBuf.map((q) => X(q.x) + ',' + Y(q.y)).join(' ')"
          class="draft" stroke="var(--accent)" stroke-dasharray="5 4" fill="none" stroke-width="2" />
        <circle v-for="(q, i) in polyBuf" :key="i" :cx="X(q.x)" :cy="Y(q.y)" :r="marker(3)" fill="var(--accent)" />
        <rect v-if="draft && (draft.kind === 'interval' || draft.kind === 'level')"
          :x="timeToX(Math.min(draft.a, draft.b))" :y="store.contentRect.y"
          :width="Math.max(2, timeToX(Math.max(draft.a, draft.b)) - timeToX(Math.min(draft.a, draft.b)))"
          :height="store.contentRect.h" class="draft" stroke="var(--accent)" stroke-dasharray="5 4"
          fill="none" stroke-width="2" />

        <!-- 时序光标 -->
        <line v-if="store.isTimeModality" :x1="cursorX" :x2="cursorX"
          :y1="store.contentRect.y" :y2="store.contentRect.y + store.contentRect.h"
          class="cursor-line" stroke="var(--accent)" stroke-width="1" />

        <!-- 拖拽实时读数（跟随光标，mono + tabular-nums） -->
        <text v-if="readout" class="stage__readout" :x="readPos.x + 10" :y="readPos.y + 4">{{ readout }}</text>
      </svg>
    </div>
  </div>
</template>

<style scoped>
.stage-wrap { position: relative; flex: 1; min-height: 0; overflow: auto; background: #0d0f12; user-select: none; }
/* BUG-011：帧图是 <img>（Blink 默认可原生拖拽）、叠加层是 SVG 内容——样式层一并禁掉，
   与 mousedown/dragstart 的 preventDefault 组成双保险 */
.stage { position: relative; margin: auto; -webkit-user-drag: none; user-select: none; }
.stage__frame { display: block; image-rendering: auto; -webkit-user-drag: none; user-select: none; }
.stage__empty { display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: var(--fs-md); }
.stage__svg { position: absolute; inset: 0; -webkit-user-drag: none; user-select: none; touch-action: none; }
.ann { cursor: pointer; }
.ann.is-sel > rect, .ann.is-sel > circle, .ann.is-sel > polygon { stroke-width: 3; }
.ann.is-drag > rect, .ann.is-drag > polygon, .ann.is-drag > circle { stroke-dasharray: 6 4; }
.ann__label { font-size: 11px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; pointer-events: none; }
.handle { cursor: nwse-resize; }
.handle__vis { fill: var(--accent); stroke: #0d0f12; stroke-width: 1; pointer-events: none; }
/* rev4「拖错可即时察觉」：胜出句柄在 hover 时就放大描边，指针进入重叠区即可看出将拖哪条边 */
.handle__vis.is-hot { fill: var(--accent-hover); stroke: var(--bg-base); stroke-width: 1.5; }
.draft { pointer-events: none; }
.cursor-line { pointer-events: none; opacity: .8; }
.stage__readout { font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: 11px; fill: var(--text-primary); paint-order: stroke; stroke: #0d0f12; stroke-width: 3; pointer-events: none; }
.stage-spinner { position: absolute; top: 50%; left: 50%; z-index: 5; transform: translate(-50%, -50%); }
.stage-spinner span { display: block; width: 14px; height: 14px; border: 2px solid var(--border-strong); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>

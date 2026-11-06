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
  if (store.modality === 'cube') { p.index = store.frameParams.index ?? 0; p.channel = store.frameParams.channel ?? 0 }
  if (store.modality === 'series' && store.frameParams.channel) p.channel = store.frameParams.channel
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
watch(displaySrc, () => { /* img onload 清 loadingFrame */ })
function onImgLoad(): void { store.loadingFrame = false }
function onImgError(): void { store.loadingFrame = false; toast.warning('帧渲染失败，可调整参数或稍后重试') }

// ---- 坐标映射：屏幕 → 帧像素 ----
function toFrame(evt: MouseEvent): { x: number; y: number } {
  const svg = svgEl.value!
  const pt = svg.createSVGPoint()
  pt.x = evt.clientX; pt.y = evt.clientY
  const m = svg.getScreenCTM()
  if (!m) return { x: 0, y: 0 }
  const p = pt.matrixTransform(m.inverse())
  return { x: p.x, y: p.y }
}
function xToTime(x: number): number {
  const { t0, t1 } = store.timeSpan
  return t0 + Math.max(0, Math.min(1, x / store.frameW)) * (t1 - t0)
}
function timeToX(t: number): number {
  const { t0, t1 } = store.timeSpan
  return ((t - t0) / (t1 - t0 || 1)) * store.frameW
}

// ---- 绘制草稿态 ----
type Draft =
  | { kind: 'box'; x1: number; y1: number; x2: number; y2: number }
  | { kind: 'interval'; a: number; b: number }
  | { kind: 'level'; a: number; b: number }
  | null
const draft = ref<Draft>(null)
const polyBuf = ref<{ x: number; y: number }[]>([])

// ---- 拖拽移动 / 缩放 / 顶点 ----
let dragLocalId: number | null = null
let dragHandle = -1 // box: 0-7 控制点, -1=整体; poly: 顶点 index
let dragStart = { x: 0, y: 0 }
let dragOrig: number[] = []

function geomAllowed(g: Geom): boolean {
  if (g === 'interval' || g === 'level') return store.isTimeModality
  return true
}

function onDown(evt: MouseEvent): void {
  if (!store.currentUri) return
  const p = toFrame(evt)
  if (store.mode === 'select') {
    const g = (evt.target as Element).closest('g.ann')
    const handle = (evt.target as Element).closest('[data-handle]')
    if (handle) {
      const id = Number(handle.closest('g.ann')?.getAttribute('data-id'))
      dragLocalId = id; dragHandle = Number(handle.getAttribute('data-handle'))
      dragStart = p; dragOrig = [...(store.draftAnns.find((a) => a.localId === id)?.coords || [])]
      return
    }
    if (g) {
      const id = Number(g.getAttribute('data-id'))
      store.select(id)
      const a = store.draftAnns.find((x) => x.localId === id)
      if (a && a.state !== 'approved') { dragLocalId = id; dragHandle = -1; dragStart = p; dragOrig = [...a.coords] }
      return
    }
    store.select(null)
    return
  }
  if (store.mode === 'point') {
    store.addAnn({ geom: 'point', coords: [Math.round(p.x), Math.round(p.y)] })
    return
  }
  if (store.mode === 'poly') {
    if (!geomAllowed('poly')) return
    polyBuf.value.push(p)
    return
  }
  if (store.mode === 'box') { draft.value = { kind: 'box', x1: p.x, y1: p.y, x2: p.x, y2: p.y }; return }
  if (store.mode === 'interval') {
    if (!geomAllowed('interval')) { toast.warning('时段仅时序态可用（Ann 模型约束）'); return }
    draft.value = { kind: 'interval', a: xToTime(p.x), b: xToTime(p.x) }; return
  }
  if (store.mode === 'level') {
    if (!geomAllowed('level')) { toast.warning('水平带仅时序态可用（Ann 模型约束）'); return }
    draft.value = { kind: 'level', a: xToTime(p.x), b: xToTime(p.x) }; return
  }
}

function onMove(evt: MouseEvent): void {
  const p = toFrame(evt)
  if (draft.value) {
    if (draft.value.kind === 'box') { draft.value.x2 = p.x; draft.value.y2 = p.y }
    else if (draft.value.kind === 'interval' || draft.value.kind === 'level') draft.value.b = xToTime(p.x)
    return
  }
  if (dragLocalId != null) {
    const a = store.draftAnns.find((x) => x.localId === dragLocalId)
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
      const d = dx / store.frameW * (store.timeSpan.t1 - store.timeSpan.t0)
      store.mutate(a.localId, { coords: [c[0] + d, c[1] + d] })
    }
    return
  }
  if (store.mode === 'select') {
    const g = (evt.target as Element).closest('g.ann')
    if (svgEl.value) svgEl.value.style.cursor = g ? 'move' : 'default'
  }
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
  if (dragLocalId != null) store.endEdit()
  dragLocalId = null; dragHandle = -1
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

defineExpose({ closePoly, cancelPoly })

// ---- 渲染辅助 ----
function colorOf(idx: number): string {
  const PALETTE = ['rgba(255,80,80,1)', 'rgba(80,200,255,1)', 'rgba(255,200,60,1)', 'rgba(120,255,120,1)', 'rgba(220,120,255,1)']
  return PALETTE[idx % PALETTE.length]
}
function stateDash(a: DraftAnn): string {
  return a.state === 'approved' ? '' : a.state === 'rejected' ? '4 3' : a.source !== 'manual' ? '6 3' : ''
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

onBeforeUnmount(() => { if (throttleTimer) clearTimeout(throttleTimer) })
</script>

<template>
  <div class="stage-wrap">
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
        @mousemove="onMove"
        @mouseup="onUp"
        @dblclick="closePoly"
      >
        <!-- 已存标注 -->
        <g
          v-for="(a, idx) in store.draftAnns"
          :key="a.localId"
          class="ann"
          :data-id="a.localId"
          :class="{ 'is-sel': a.localId === store.selId }"
        >
          <template v-if="a.geom === 'box'">
            <rect :x="Math.min(a.coords[0], a.coords[2])" :y="Math.min(a.coords[1], a.coords[3])"
              :width="Math.abs(a.coords[2] - a.coords[0])" :height="Math.abs(a.coords[3] - a.coords[1])"
              :stroke="colorOf(idx)" :stroke-dasharray="stateDash(a)" fill="none" stroke-width="2" />
            <text :x="Math.min(a.coords[0], a.coords[2]) + 3" :y="Math.min(a.coords[1], a.coords[3]) - 4"
              class="ann__label" :fill="colorOf(idx)">{{ a.label || a.geom }}</text>
            <template v-if="a.localId === store.selId && store.mode === 'select'">
              <rect v-for="hd in boxHandles(a.coords)" :key="hd.h" :data-handle="hd.h"
                :x="hd.x - 4" :y="hd.y - 4" width="8" height="8" class="handle" :fill="colorOf(idx)" />
            </template>
          </template>
          <template v-else-if="a.geom === 'point'">
            <circle :cx="a.coords[0]" :cy="a.coords[1]" r="6" :stroke="colorOf(idx)"
              :stroke-dasharray="stateDash(a)" fill="rgba(0,0,0,.3)" stroke-width="2" />
          </template>
          <template v-else-if="a.geom === 'poly'">
            <polygon :points="a.coords.reduce((acc, v, i) => acc + (i % 2 ? ',' + v : (acc ? ' ' : '') + v), '')"
              :stroke="colorOf(idx)" :stroke-dasharray="stateDash(a)" fill="none" stroke-width="2" />
            <template v-if="a.localId === store.selId && store.mode === 'select'">
              <circle v-for="(_, vi) in (a.coords.length / 2)" :key="vi" :data-handle="vi"
                :cx="a.coords[vi * 2]" :cy="a.coords[vi * 2 + 1]" r="4" class="handle" :fill="colorOf(idx)" />
            </template>
          </template>
          <template v-else-if="a.geom === 'interval'">
            <rect :x="timeToX(a.coords[0])" :y="0" :width="Math.max(2, timeToX(a.coords[1]) - timeToX(a.coords[0]))"
              :height="store.frameH" :stroke="colorOf(idx)" fill="rgba(76,141,246,.12)"
              :stroke-dasharray="stateDash(a)" stroke-width="1.5" />
          </template>
          <template v-else-if="a.geom === 'level'">
            <rect :x="timeToX(a.coords[0])" :y="0" :width="Math.max(2, timeToX(a.coords[1]) - timeToX(a.coords[0]))"
              :height="store.frameH" :stroke="colorOf(idx)" fill="rgba(217,160,63,.14)"
              stroke-dasharray="2 2" stroke-width="1.5" />
          </template>
        </g>

        <!-- 绘制中草稿 -->
        <rect v-if="draft && draft.kind === 'box'" :x="Math.min(draft.x1, draft.x2)" :y="Math.min(draft.y1, draft.y2)"
          :width="Math.abs(draft.x2 - draft.x1)" :height="Math.abs(draft.y2 - draft.y1)"
          class="draft" stroke="var(--accent)" stroke-dasharray="5 4" fill="none" stroke-width="2" />
        <polygon v-if="polyBuf.length" :points="polyBuf.map((q) => q.x + ',' + q.y).join(' ')"
          class="draft" stroke="var(--accent)" stroke-dasharray="5 4" fill="rgba(76,141,246,.08)" stroke-width="2" />
        <circle v-for="(q, i) in polyBuf" :key="i" :cx="q.x" :cy="q.y" r="3" fill="var(--accent)" />
        <rect v-if="draft && (draft.kind === 'interval' || draft.kind === 'level')"
          :x="timeToX(Math.min(draft.a, draft.b))" :y="0"
          :width="Math.max(2, timeToX(Math.max(draft.a, draft.b)) - timeToX(Math.min(draft.a, draft.b)))"
          :height="store.frameH" class="draft" stroke="var(--accent)" stroke-dasharray="5 4"
          fill="rgba(76,141,246,.12)" stroke-width="2" />

        <!-- 时序光标 -->
        <line v-if="store.isTimeModality" :x1="cursorX" :x2="cursorX" y1="0" :y2="store.frameH"
          class="cursor-line" stroke="var(--accent)" stroke-width="1" />
      </svg>
    </div>
  </div>
</template>

<style scoped>
.stage-wrap { position: relative; flex: 1; min-height: 0; overflow: auto; background: #0d0f12; }
.stage { position: relative; margin: auto; }
.stage__frame { display: block; image-rendering: auto; }
.stage__empty { display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: var(--fs-md); }
.stage__svg { position: absolute; inset: 0; }
.ann { cursor: pointer; }
.ann.is-sel rect, .ann.is-sel circle, .ann.is-sel polygon { stroke-width: 3; }
.ann__label { font-size: 11px; font-family: var(--font-mono); pointer-events: none; }
.handle { cursor: nwse-resize; stroke: #0d0f12; stroke-width: 1; }
.draft { pointer-events: none; }
.cursor-line { pointer-events: none; opacity: .8; }
.stage-spinner { position: absolute; top: 50%; left: 50%; z-index: 5; transform: translate(-50%, -50%); }
.stage-spinner span { display: block; width: 14px; height: 14px; border: 2px solid var(--border-strong); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>

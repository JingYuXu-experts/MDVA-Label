<script setup lang="ts">
import { computed, ref } from 'vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSlider from '@/components/ui/MSlider.vue'
import { useCanvasStore } from '@/stores/canvas'

const store = useCanvasStore()

const STRETCH_OPTIONS = [
  { value: 'linear', label: 'linear' },
  { value: 'sqrt', label: 'sqrt' },
  { value: 'power', label: 'power' },
  { value: 'gamma', label: 'gamma' },
  { value: 'log', label: 'log' },
  { value: 'asinh', label: 'asinh' },
  { value: 'histeq', label: 'histeq' },
  { value: 'clahe', label: 'clahe' },
]
const COLORMAP_OPTIONS = [
  { value: 'gray', label: 'gray' },
  { value: 'viridis', label: 'viridis' },
  { value: 'inferno', label: 'inferno' },
  { value: 'plasma', label: 'plasma' },
  { value: 'hot', label: 'hot' },
  { value: 'cool', label: 'cool' },
]

const fitTitle = computed(() => {
  const f = store.fit
  if (!f) return '无 letterbox：Ann 坐标空间 = 出图画布'
  return `fit=[${f.x0},${f.y0},${f.w},${f.h},${f.scale.toFixed(4)}]　Ann 坐标=native 图像像素，叠加层按此映射`
})

const channelOpts = computed(() =>
  store.channels.length
    ? store.channels.map((c, i) => ({ value: i, label: String(c) }))
    : Array.from({ length: Math.max(1, store.nViews) }, (_, i) => ({ value: i, label: `CH${i}` })))

const isCube = computed(() => store.modality === 'cube')
const isSeries = computed(() => store.modality === 'series')
const isTime = computed(() => store.isTimeModality)

// ---- 时间轴：光标本地即时，帧请求由 store 节流 ----
const axisEl = ref<HTMLElement | null>(null)
let axisDragging = false
function axisToTime(clientX: number): number {
  const el = axisEl.value
  if (!el) return store.timeSpan.t0
  const r = el.getBoundingClientRect()
  const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width))
  return store.timeSpan.t0 + frac * (store.timeSpan.t1 - store.timeSpan.t0)
}
function moveCursor(clientX: number): void {
  store.setParam('cursor', Number(axisToTime(clientX).toFixed(3)))
}
function axisDown(e: MouseEvent): void {
  axisDragging = true
  moveCursor(e.clientX)
  const up = () => { axisDragging = false; window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up) }
  const mv = (ev: MouseEvent) => { if (axisDragging) moveCursor(ev.clientX) }
  window.addEventListener('mousemove', mv)
  window.addEventListener('mouseup', up)
}

const cursorPct = computed(() => {
  const { t0, t1 } = store.timeSpan
  const c = store.frameParams.cursor ?? t0
  return ((c - t0) / (t1 - t0 || 1)) * 100
})
const cursorAt = computed(() => store.frameParams.cursor ?? store.timeSpan.t0)

// ---- 已有时段/水平带在时间轴上的投影 ----
const bands = computed(() =>
  store.draftAnns
    .filter((a) => a.geom === 'interval' || a.geom === 'level')
    .map((a) => {
      const { t0, t1 } = store.timeSpan
      const l = ((a.coords[0] - t0) / (t1 - t0 || 1)) * 100
      const w = ((a.coords[1] - a.coords[0]) / (t1 - t0 || 1)) * 100
      return { id: a.localId, left: Math.max(0, l), width: Math.max(0.5, w), level: a.geom === 'level' }
    }))

function fmt2(v: number): string { return v.toFixed(2) }
function fmtIndex(v: number): string { return `${Math.round(v)}/${store.nViews}` }

// ---- 时间显示（UI规格 v1.4 审查项②）：主显本地时间，epoch 进 title ----
const pad = (n: number) => String(n).padStart(2, '0')
function fmtLocal(t: number): string {
  if (!Number.isFinite(t)) return '--'
  const d = new Date(t * 1000)
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}
function fmtEpoch(t: number): string {
  return Number.isFinite(t) ? `epoch ${t.toFixed(3)} s` : 'epoch --'
}

// ---- 轴刻度按跨度自动选级 ----
const STEPS = [0.1, 0.25, 0.5, 1, 5, 15, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 259200, 604800, 2592000]
const ticks = computed(() => {
  const { t0, t1 } = store.timeSpan
  const span = t1 - t0
  if (!(span > 0) || !Number.isFinite(t0)) return []
  const step = STEPS.find((s) => span / s <= 6) || STEPS[STEPS.length - 1]
  const out: { left: number; label: string }[] = []
  const first = Math.ceil(t0 / step) * step
  for (let t = first; t <= t1 + 1e-9 && out.length < 12; t += step) {
    const d = new Date(t * 1000)
    let label: string
    if (step >= 86400) label = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    else if (step >= 3600) label = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`
    else if (step >= 1) label = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    else label = `${pad(d.getSeconds())}.${pad(Math.round((t % 1) * 100))}`
    out.push({ left: ((t - t0) / span) * 100, label })
  }
  return out
})
</script>

<template>
  <div class="parambar">
    <div class="parambar__controls">
      <template v-if="isSeries">
        <span class="lbl">通道</span>
        <MSelect :model-value="store.frameParams.channel ?? 0" :options="channelOpts" width="96px"
          @update:model-value="store.setParam('channel', $event)" />
      </template>
      <template v-if="isCube">
        <span class="lbl">切片</span>
        <span class="slider-wrap">
          <MSlider :model-value="store.frameParams.index ?? 0" :min="0" :max="Math.max(0, store.nViews - 1)" :step="1"
            :format="fmtIndex" @update:model-value="store.setParam('index', $event)" />
        </span>
      </template>
      <span class="lbl">stretch</span>
      <MSelect :model-value="store.frameParams.stretch ?? 'linear'" :options="STRETCH_OPTIONS" width="104px"
        @update:model-value="store.setParam('stretch', String($event))" />
      <template v-if="isCube">
        <span class="lbl">colormap</span>
        <MSelect :model-value="store.frameParams.colormap ?? 'gray'" :options="COLORMAP_OPTIONS" width="96px"
          @update:model-value="store.setParam('colormap', String($event))" />
      </template>
      <span class="lbl">gamma</span>
      <span class="slider-wrap">
        <MSlider :model-value="store.frameParams.gamma ?? 1" :min="0.1" :max="2" :step="0.05" :format="fmt2"
          @update:model-value="store.setParam('gamma', $event)" />
      </span>
      <span class="parambar__meta num" :title="fitTitle">
        {{ store.modality }} · {{ Math.round(store.srcW) }}×{{ Math.round(store.srcH) }}
        <span class="parambar__fit">画布 {{ store.frameW }}×{{ store.frameH }}</span>
      </span>
    </div>
    <div v-if="isTime" ref="axisEl" class="axis" role="slider" tabindex="0"
      aria-label="时间轴光标" :aria-valuetext="fmtLocal(cursorAt)" :aria-valuemin="store.timeSpan.t0"
      :aria-valuemax="store.timeSpan.t1" :aria-valuenow="cursorAt" @mousedown="axisDown">
      <div v-for="b in bands" :key="b.id" class="axis__band" :class="{ 'is-level': b.level }"
        :style="{ left: b.left + '%', width: b.width + '%' }" />
      <div v-for="(tk, i) in ticks" :key="i" class="axis__tick" :style="{ left: tk.left + '%' }">
        <span class="axis__ticklabel num">{{ tk.label }}</span>
      </div>
      <div class="axis__cursor" :style="{ left: cursorPct + '%' }" />
      <span class="axis__read num" :title="fmtEpoch(cursorAt)">{{ fmtLocal(cursorAt) }}</span>
    </div>
  </div>
</template>

<style scoped>
.parambar { flex: none; background: var(--bg-panel); border-top: 1px solid var(--border); }
.parambar__controls { display: flex; align-items: center; gap: var(--sp-2); padding: 6px var(--sp-3); flex-wrap: wrap; }
.lbl { font-size: var(--fs-xs); color: var(--text-secondary); }
.slider-wrap { width: 120px; }
.val { font-size: var(--fs-xs); color: var(--text-secondary); min-width: 34px; }
.parambar__meta { margin-left: auto; font-size: var(--fs-xs); color: var(--text-muted); }
.parambar__fit { margin-left: 6px; opacity: .75; }
.axis { position: relative; height: 44px; background: var(--bg-raised); border-top: 1px solid var(--border); cursor: crosshair; user-select: none; }
.axis__band { position: absolute; top: 0; bottom: 0; background: rgba(76,141,246,.22); border-left: 1px solid var(--accent); }
.axis__band.is-level { background: rgba(217,160,63,.22); border-left-color: var(--warning); }
.axis__cursor { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); transform: translateX(-1px); }
.axis__read { position: absolute; right: 8px; top: 4px; font-size: var(--fs-xs); color: var(--text-primary); background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius-control); padding: 0 var(--sp-1); }
.axis__tick { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--border-strong); opacity: .7; pointer-events: none; }
.axis__ticklabel { position: absolute; left: 3px; bottom: 3px; font-size: 10px; line-height: 1; color: var(--text-muted); white-space: nowrap; }
</style>

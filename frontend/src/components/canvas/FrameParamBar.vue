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

function fmtT(t: number): string {
  return t.toFixed(2)
}
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
            @update:model-value="store.setParam('index', $event)" />
        </span>
        <span class="num val">{{ store.frameParams.index ?? 0 }}/{{ store.nViews }}</span>
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
        <MSlider :model-value="store.frameParams.gamma ?? 1" :min="0.1" :max="2" :step="0.05"
          @update:model-value="store.setParam('gamma', $event)" />
      </span>
      <span class="num val">{{ (store.frameParams.gamma ?? 1).toFixed(2) }}</span>
      <span class="parambar__meta num">{{ store.modality }} · {{ store.frameW }}×{{ store.frameH }}</span>
    </div>
    <div v-if="isTime" ref="axisEl" class="axis" role="slider" tabindex="0"
      aria-label="时间轴光标" :aria-valuenow="store.frameParams.cursor ?? 0" @mousedown="axisDown">
      <div v-for="b in bands" :key="b.id" class="axis__band" :class="{ 'is-level': b.level }"
        :style="{ left: b.left + '%', width: b.width + '%' }" />
      <div class="axis__cursor" :style="{ left: cursorPct + '%' }" />
      <span class="axis__read num">t = {{ fmtT(store.frameParams.cursor ?? store.timeSpan.t0) }} s</span>
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
.axis { position: relative; height: 44px; background: var(--bg-raised); border-top: 1px solid var(--border); cursor: crosshair; user-select: none; }
.axis__band { position: absolute; top: 0; bottom: 0; background: rgba(76,141,246,.22); border-left: 1px solid var(--accent); }
.axis__band.is-level { background: rgba(217,160,63,.22); border-left-color: var(--warning); }
.axis__cursor { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); transform: translateX(-1px); }
.axis__read { position: absolute; right: 8px; top: 4px; font-size: var(--fs-xs); color: var(--text-secondary); }
</style>

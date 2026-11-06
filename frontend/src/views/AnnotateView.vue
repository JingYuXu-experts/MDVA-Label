<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { GitCompareArrows } from 'lucide-vue-next'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import MDialog from '@/components/ui/MDialog.vue'
import QueueRail from '@/components/canvas/QueueRail.vue'
import CanvasToolbar from '@/components/canvas/CanvasToolbar.vue'
import CanvasStage from '@/components/canvas/CanvasStage.vue'
import FrameParamBar from '@/components/canvas/FrameParamBar.vue'
import AnnListPanel from '@/components/canvas/AnnListPanel.vue'
import DiffDialog from '@/components/canvas/DiffDialog.vue'
import { useCanvasStore } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore } from '@/stores/items'
import * as api from '@/api/endpoints'

const store = useCanvasStore()
const ctx = useCtxStore()
const items = useItemsStore()
const route = useRoute()

const stageRef = ref<InstanceType<typeof CanvasStage> | null>(null)
const labelOptions = ref<string[]>([])
const diffOpen = ref(false)
const leavePrompt = ref(false)
let pendingLeave: (() => void) | null = null
let pendingRoute: (() => void) | null = null

const versionOpts = computed(() =>
  ctx.versions.map((v) => ({ value: v.id, label: `${v.op} · ${v.id.slice(1, 7)}`, hint: v.id === ctx.head ? '当前' : '' })))

async function loadLabels(): Promise<void> {
  if (!ctx.dataset) { labelOptions.value = []; return }
  try {
    const ds = await api.getDataset(ctx.dataset)
    const ls = (ds.labels as { name: string; color?: string }[]) || []
    store.setLabelColors(ls)
    labelOptions.value = ls.map((l) => l.name).filter(Boolean)
    if (!store.label && labelOptions.value.length) store.label = labelOptions.value[0]
  } catch { labelOptions.value = [] }
}

async function ensureQueue(): Promise<void> {
  if (!items.items.length && ctx.dataset && ctx.version) await items.fetchItems()
}

async function openUri(uri: string): Promise<void> {
  if (!uri) return
  await store.openItem(uri)
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', onBeforeUnload)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// ctx 就绪后再初始化队列/标签/当前条目（避免与 App boot 竞态）
let bootstrapped = false

/**
 * 出图尺寸覆盖：`?outw=&outh=`（128–2048，越界取边界值）。
 * 用途是 v1.4 句柄用例⑤「跨参数漂移不变」——同一份 Ann 坐标在
 * out_width/height 不同（→ meta.fit 不同）时必须仍钉在同一图像位置上。
 */
function applyOutSizeFromQuery(): void {
  const pick = (k: string): number | null => {
    const v = Number(route.query[k])
    return Number.isFinite(v) && v > 0 ? Math.min(2048, Math.max(128, Math.round(v))) : null
  }
  const w = pick('outw')
  const h = pick('outh')
  if (w) store.frameW = w
  if (h) store.frameH = h
}

async function bootstrap(): Promise<void> {
  if (bootstrapped || !ctx.dataset || !ctx.version) return
  bootstrapped = true
  applyOutSizeFromQuery()
  await Promise.all([ensureQueue(), loadLabels()])
  const q = route.query.uri
  const target = typeof q === 'string' && q ? q
    : (store.currentUri || items.items[0]?.uri || '')
  if (target) await openUri(target)
}
watch(() => [ctx.dataset, ctx.version], bootstrap, { immediate: true })

// 版本切换 → 整体刷新（annotations/frame 按 version 取）
watch(() => ctx.version, async () => {
  await ensureQueue()
  if (store.currentUri) await openUri(store.currentUri)
})
watch(() => ctx.dataset, loadLabels)

// ---- 条目切换（含未保存拦截）----
async function pickUri(uri: string): Promise<void> {
  if (uri === store.currentUri) return
  if (store.dirty) {
    pendingLeave = () => void openUri(uri)
    leavePrompt.value = true
    return
  }
  await openUri(uri)
}

function saveAndStay(): void { leavePrompt.value = false; pendingLeave = null; pendingRoute = null }
async function saveAndContinue(): Promise<void> {
  const ok = await store.save()
  if (!ok || store.dirty) return
  leavePrompt.value = false
  const fn = pendingLeave; const rt = pendingRoute
  pendingLeave = null; pendingRoute = null
  if (fn) fn(); else if (rt) rt()
}
function discardAndLeave(): void {
  leavePrompt.value = false
  store.dirty = false
  const fn = pendingLeave; const rt = pendingRoute
  pendingLeave = null; pendingRoute = null
  if (fn) fn(); else if (rt) rt()
}

// ---- 路由离开拦截（未保存）----
onBeforeRouteLeave((_to, _from, next) => {
  if (!store.dirty) { next(true); return }
  pendingRoute = () => next(true)
  leavePrompt.value = true
  next(false)
})

function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (store.dirty) { e.preventDefault(); e.returnValue = '' }
}

// ---- 快捷键 ----
const KEYMODE: Record<string, 'select' | 'box' | 'poly' | 'point' | 'interval' | 'level'> = {
  '1': 'select', '2': 'box', '3': 'poly', '4': 'point', '5': 'interval', '6': 'level',
}
function onKey(e: KeyboardEvent): void {
  const tag = (e.target as HTMLElement)?.tagName
  if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') return
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); void store.save(); return }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) store.redo(); else store.undo(); return }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); store.redo(); return }
  if (e.key === 'Delete' || e.key === 'Backspace') { if (store.selId != null) store.deleteAnn(store.selId); return }
  if (e.key === 'Escape') { if (stageRef.value?.cancelVertexEdit()) return; stageRef.value?.cancelPoly(); store.select(null); return }
  if (e.key === 'Enter') { if (stageRef.value?.commitVertex()) return; stageRef.value?.closePoly(); return }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { void step(e.key === 'ArrowRight' ? 1 : -1); return }
  if (e.key === '+' || e.key === '=') { store.zoom = Math.min(8, store.zoom + 0.2); return }
  if (e.key === '-' || e.key === '_') { store.zoom = Math.max(0.2, store.zoom - 0.2); return }
  if (e.key === '0') { store.zoom = 1; return }
  if (KEYMODE[e.key]) store.setMode(KEYMODE[e.key])
}
async function step(dir: number): Promise<void> {
  const list = items.items
  if (!list.length) return
  const idx = list.findIndex((x) => x.uri === store.currentUri)
  const next = list[(idx + dir + list.length) % list.length]
  await pickUri(next.uri)
}
</script>

<template>
  <div class="annotate">
    <div class="annotate__top">
      <MSelect :model-value="ctx.version" :options="versionOpts" label="版本" width="220px"
        @update:model-value="ctx.setVersion(String($event))" />
      <span v-if="store.currentUri" class="annotate__uri mono" :title="store.currentUri">{{ store.currentUri }}</span>
      <span class="annotate__spacer" />
      <MButton size="sm" variant="ghost" @click="diffOpen = true"><GitCompareArrows :size="14" /> 版本对比</MButton>
    </div>
    <div class="annotate__body">
      <QueueRail @pick="pickUri" />
      <div class="annotate__main">
        <CanvasToolbar :label-options="labelOptions" />
        <CanvasStage ref="stageRef" />
        <FrameParamBar />
      </div>
      <AnnListPanel />
    </div>

    <DiffDialog :open="diffOpen" @close="diffOpen = false" />

    <MDialog :open="leavePrompt" title="未保存的修改" :width="480" @close="saveAndStay">
      <p class="leave__msg">当前条目有未保存修改，仍要离开吗？保存将整屏覆盖式提交（POST /api/annotations，草稿态）。</p>
      <template #footer>
        <MButton variant="ghost" @click="saveAndStay">继续编辑</MButton>
        <MButton variant="danger" @click="discardAndLeave">放弃修改</MButton>
        <MButton variant="primary" @click="saveAndContinue">保存并继续</MButton>
      </template>
    </MDialog>
  </div>
</template>

<style scoped>
.annotate { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.annotate__top { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2) var(--sp-3); background: var(--bg-panel); border-bottom: 1px solid var(--border); flex: none; }
.annotate__uri { font-size: var(--fs-xs); color: var(--text-secondary); max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.annotate__spacer { flex: 1; }
.annotate__body { display: flex; flex: 1; min-height: 0; }
.annotate__main { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.leave__msg { font-size: var(--fs-ui); color: var(--text-secondary); margin: 0; }
</style>

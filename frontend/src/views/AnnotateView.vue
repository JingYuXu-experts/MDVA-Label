<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import type { NavigationGuardNext } from 'vue-router'
import { GitCompareArrows } from 'lucide-vue-next'
import MSelect from '@/components/ui/MSelect.vue'
import MButton from '@/components/ui/MButton.vue'
import MDialog from '@/components/ui/MDialog.vue'
import QueueRail from '@/components/canvas/QueueRail.vue'
import CanvasToolbar from '@/components/canvas/CanvasToolbar.vue'
import ReferenceBar from '@/components/canvas/ReferenceBar.vue'
import SolarModelPanel from '@/components/canvas/SolarModelPanel.vue'
import SolarInputGuide from '@/components/SolarInputGuide.vue'
import CanvasStage from '@/components/canvas/CanvasStage.vue'
import FrameParamBar from '@/components/canvas/FrameParamBar.vue'
import AnnListPanel from '@/components/canvas/AnnListPanel.vue'
import DiffDialog from '@/components/canvas/DiffDialog.vue'
import { useCanvasStore } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'
import { useItemsStore } from '@/stores/items'
import { bucketOf } from '@/stores/items'
import * as api from '@/api/endpoints'
import WorkflowSteps from '@/components/layout/WorkflowSteps.vue'
import { useToast } from '@/stores/toast'

const store = useCanvasStore()
const ctx = useCtxStore()
const items = useItemsStore()
const route = useRoute()

const stageRef = ref<InstanceType<typeof CanvasStage> | null>(null)
const queueRef = ref<InstanceType<typeof QueueRail> | null>(null)
const openError=ref('')
const labelOptions = ref<string[]>([])
const diffOpen = ref(false)
const leavePrompt = ref(false)
let pendingLeave: (() => void) | null = null
let pendingRoute: NavigationGuardNext | null = null

const versionOpts = computed(() =>
  ctx.versions.map((v) => ({ value: v.id, label: `${v.op} · ${v.id.slice(1, 7)}`, hint: v.id === ctx.head ? '当前' : '' })))

async function loadLabels(): Promise<void> {
  if (!ctx.dataset) { labelOptions.value = []; return }
  const dataset=ctx.dataset
  try {
    const ds = await api.getDataset(dataset)
    if(dataset!==ctx.dataset)return
    const ls = (ds.labels as { name: string; color?: string }[]) || []
    store.setLabelColors(ls)
    labelOptions.value = ls.map((l) => l.name).filter(Boolean)
    if (!labelOptions.value.includes(store.label) && labelOptions.value.length) store.label = labelOptions.value[0]
  } catch { labelOptions.value = [] }
}

async function openUri(uri: string): Promise<void> {
  if (!uri) return
  openError.value=''
  try{await store.openItem(uri)}catch(e){if(uri===store.currentUri)openError.value=String(e)}
}

onMounted(() => {
  ctx.setContextGuard(() => {if(store.saving){useToast().info('正在保存，请稍后再切换数据。');return false}return !store.dirty || window.confirm('当前图片有未保存的修改。切换数据集或版本将放弃这些修改，仍要切换吗？')})
  window.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', onBeforeUnload)
})
onBeforeUnmount(() => {
  ctx.setContextGuard(null)
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// ctx 就绪后再初始化队列/标签/当前条目（避免与 App boot 竞态）
let bootstrapSeq = 0

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

watch(() => [ctx.ws,ctx.dataset,ctx.version], async (context,previous) => {
  const run=++bootstrapSeq
  if(!previous||context[0]!==previous[0]||context[1]!==previous[1]){store.reset();items.reset()}
  if(!ctx.dataset||!ctx.version||store.saving)return
  applyOutSizeFromQuery()
  await Promise.all([items.fetchItems(),loadLabels()])
  if(run!==bootstrapSeq)return
  const q=route.query.dataset===ctx.dataset?route.query.uri:''
  const target=typeof q==='string'&&q?q:items.items.some(i=>i.uri===store.currentUri)?store.currentUri:items.items[0]?.uri||''
  if(target)await openUri(target)
}, {immediate:true})

// ---- 条目切换（含未保存拦截）----
async function pickUri(uri: string): Promise<void> {
  if(store.saving){useToast().info('正在保存，请稍后切换图片。');return}
  if (uri === store.currentUri) return
  if (store.dirty) {
    pendingLeave = () => void openUri(uri)
    leavePrompt.value = true
    return
  }
  await openUri(uri)
}

function saveAndStay(): void { leavePrompt.value = false; pendingLeave = null; pendingRoute?.(false);pendingRoute = null }
async function saveAndContinue(): Promise<void> {
  const ok = await store.save()
  if (!ok || store.dirty) return
  leavePrompt.value = false
  const fn = pendingLeave; const rt = pendingRoute
  pendingLeave = null; pendingRoute = null
  if (fn) fn(); else if (rt) rt(true)
}
function discardAndLeave(): void {
  leavePrompt.value = false
  store.dirty = false
  const fn = pendingLeave; const rt = pendingRoute
  pendingLeave = null; pendingRoute = null
  if (fn) fn(); else if (rt) rt(true)
}

// ---- 路由离开拦截（未保存）----
onBeforeRouteLeave((_to, _from, next) => {
  if(store.saving){useToast().info('正在保存，请稍后离开。');next(false);return}
  if (!store.dirty) { next(true); return }
  pendingRoute = next
  leavePrompt.value = true
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
  if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA'||document.querySelector('[role="dialog"]')) return
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
  if(store.saving||store.loading)return
  const action=async()=>{if(items.loading)await items.refreshSavedItems();const uri=await queueRef.value?.adjacent(dir);if(uri)await openUri(uri)}
  if(store.dirty){pendingLeave=()=>void action();leavePrompt.value=true;return}
  await action()
}
async function saveNext(){if(await store.save()){if(!store.dirty)await step(1)}}
function syncQueueEntry(){
  const item=items.items.find(i=>i.uri===store.currentUri)
  if(item){const before=bucketOf(item);item.n_annotations=store.draftAnns.length;item.states={draft:0,submitted:0,approved:0,rejected:0};for(const a of store.draftAnns)item.states[a.state]++;const after=bucketOf(item);if(before!==after){items.serverCounts[before]=Math.max(0,(items.serverCounts[before]||0)-1);items.serverCounts[after]=(items.serverCounts[after]||0)+1}}
}
watch(()=>[store.loading,store.saving,store.dirty,store.currentUri,store.draftAnns.map(a=>a.state).join(',')],()=>{if(!store.loading&&!store.saving&&!store.dirty)syncQueueEntry()})
watch(()=>store.saving,(saving,previous)=>{if(previous&&!saving&&!store.dirty)void items.refreshSavedItems()})
</script>

<template>
  <div class="annotate">
    <WorkflowSteps />
    <div class="annotate__top">
      <MSelect :model-value="ctx.version" :options="versionOpts" label="版本" width="220px"
        @update:model-value="ctx.setVersion(String($event))" />
      <span v-if="store.currentUri" class="annotate__uri mono" :title="store.currentUri">{{ store.currentUri }}</span>
      <span class="annotate__spacer" />
      <MButton size="sm" variant="ghost" :disabled="store.loading||store.saving||items.loading" @click="step(-1)">上一张</MButton>
      <MButton size="sm" variant="ghost" :disabled="store.loading||store.saving||items.loading" @click="step(1)">下一张</MButton>
      <MButton size="sm" :disabled="store.loading||store.saving||!store.currentUri" @click="saveNext">保存并下一张</MButton>
      <MButton size="sm" variant="ghost" @click="diffOpen = true"><GitCompareArrows :size="14" /> 版本对比</MButton>
    </div>
    <div class="annotate__body">
      <QueueRail ref="queueRef" @pick="pickUri" />
      <div class="annotate__main">
        <p v-if="openError" class="annotation-error" role="alert">图片打开失败：{{openError}} <button @click="openUri(store.currentUri)">重试</button></p>
        <SolarInputGuide />
        <CanvasToolbar :label-options="labelOptions" @labels-changed="loadLabels" />
        <ReferenceBar />
        <SolarModelPanel @pick="pickUri" />
        <CanvasStage ref="stageRef" />
        <FrameParamBar />
      </div>
      <AnnListPanel />
    </div>

    <DiffDialog :open="diffOpen" @close="diffOpen = false" />

    <MDialog :open="leavePrompt" title="未保存的修改" :width="480" @close="saveAndStay">
      <p class="leave__msg">这张图片的标注还没有保存。保存后可以继续到下一张图片或下一步；选择放弃则不会保留本次修改。</p>
      <p v-if="store.saveError" class="annotation-error" role="alert">保存失败：{{store.saveError}}。修改仍保留在画布中，可以重试。</p>
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
.annotation-error{padding:10px;color:var(--danger);font-size:12px}.annotate__top{flex-wrap:wrap}.annotate__main{min-height:0}@media(max-width:1000px){.annotate{overflow:auto}.annotate__body{flex-wrap:wrap;flex:none;min-height:650px}.annotate__main{min-width:300px;min-height:550px}.annotate__body>:last-child{width:100%;max-height:300px}}@media(max-width:600px){.annotate__body{flex-direction:column}.annotate__body :deep(.rail){width:100%;height:300px}.annotate__uri{max-width:100%}}
</style>

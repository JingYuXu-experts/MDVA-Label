<script setup lang="ts">
import { computed, ref } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MSelect from '@/components/ui/MSelect.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import KV from '@/components/ui/KV.vue'
import { useCanvasStore } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'
import * as api from '@/api/endpoints'

const store = useCanvasStore()
const ctx = useCtxStore()

const open = ref(false)
const workers = ref(4)
const backend = ref('cosmic_reject')
const out = ref('')
const speedup = ref<Record<string, unknown> | null>(null)
const pre = ref<{ n_items: number; n_annotations: number; n_ok: number } | null>(null)
const samePreview = ref<number>(0)
const busy = ref(false)

const selBox = computed(() =>
  store.selected && store.selected.geom === 'box' ? store.selected : null)

const smartParams = computed(() => ({
  backend: backend.value, workers: workers.value, role: ctx.role,
}))

async function onSameImage(): Promise<void> {
  if (!selBox.value) return
  busy.value = true
  try {
    const resp = await api.smartSameImage(store.currentUri, {
      ...smartParams.value, coords: selBox.value.coords,
    })
    samePreview.value = resp.n
    // 采纳：勾选后并入草稿（此处一键并入全部候选，虚线预览→落草稿）
    for (const a of resp.anns) {
      if (a.geom === 'box') {
        store.addAnn({ geom: 'box', coords: (a.coords || []).map(Number), label: a.label || store.label, source: 'visual_prompt', state: 'draft' })
      }
    }
    out.value = `同图找相似：命中 ${resp.n} 个候选，已并入草稿（未直接落库，保存后生效）`
  } catch (e) {
    out.value = `同图失败：${e instanceof Error ? e.message : String(e)}`
  } finally { busy.value = false }
}

async function onPreannotate(): Promise<void> {
  busy.value = true
  try {
    const resp = await api.smartPreannotate([store.currentUri], smartParams.value)
    pre.value = { n_items: resp.n_items, n_annotations: resp.n_annotations, n_ok: resp.n_ok }
    out.value = `干跑：将为 ${resp.n_items} 路产生 ${resp.n_annotations} 框（POST /smart/preannotate，不落库）`
  } catch (e) {
    out.value = `干跑失败：${e instanceof Error ? e.message : String(e)}`
  } finally { busy.value = false }
}

async function onApply(): Promise<void> {
  out.value = '正在落库并生成新版本…'
  const ok = await store.smartApply(smartParams.value)
  if (ok) out.value = '已落新版本（人工标注已保留并 carry）；「撤销机器结果」已绑定新 version_id'
  else out.value = 'apply 失败，未产生新版本'
}

async function onUndo(): Promise<void> {
  await store.smartUndo()
  out.value = 'head 指回父版本（不删除任何数据）'
}

async function onSpeedup(): Promise<void> {
  busy.value = true
  try {
    speedup.value = await api.smartSpeedup([store.currentUri], { ...smartParams.value })
    out.value = '并行加速比（论文实验数据）'
  } catch (e) {
    out.value = `加速比失败：${e instanceof Error ? e.message : String(e)}`
  } finally { busy.value = false }
}

const speedupNum = computed(() => {
  const s = speedup.value
  if (!s) return ''
  return String(s.speedup ?? s.ratio ?? '—')
})

async function onActiveRound(): Promise<void> {
  if (!ctx.version) return
  busy.value = true
  try {
    const resp = await api.smartActiveRound(ctx.version, [], { ...smartParams.value, budget: 20 })
    const chosen = (resp.chosen as unknown[]) || []
    out.value = `主动学习一轮：选样 ${chosen.length} 条（不确定性排序，只读不落库）`
  } catch (e) {
    out.value = `主动学习失败：${e instanceof Error ? e.message : String(e)}`
  } finally { busy.value = false }
}
</script>

<template>
  <details class="smart" :open="open" @toggle="open = ($event.target as HTMLDetailsElement).open">
    <summary>智能标注（预标注 / 同图 / 加速比 / 主动学习）</summary>
    <div class="smart__body">
      <div class="row">
        <span class="lbl">后端</span>
        <MSelect :model-value="backend" :options="[{ value: 'cosmic_reject', label: 'cosmic_reject' }, { value: 'visual_prompt', label: 'visual_prompt' }]" width="150px" @update:model-value="backend = String($event)" />
        <span class="lbl">workers</span>
        <NumberInput v-model="workers" :min="0" :max="16" :step="1" />
      </div>
      <div class="btns">
        <MButton size="sm" variant="secondary" :disabled="!selBox || busy" :title="selBox ? '给一个框，找同图同类' : '先选中一个 box'" @click="onSameImage">同图找相似</MButton>
        <MButton size="sm" variant="secondary" :disabled="busy || !store.currentUri" @click="onPreannotate">批量预标注（干跑）</MButton>
        <MButton size="sm" variant="primary" :disabled="busy || !store.currentUri" @click="onApply">应用并落库</MButton>
        <MButton size="sm" variant="danger" :disabled="!store.smartAppliedVersion || busy" :title="store.smartAppliedVersion ? '回退到父版本，不删除任何数据' : 'apply 成功后才可撤销'" @click="onUndo">撤销机器结果</MButton>
      </div>
      <div v-if="pre" class="stat num">干跑：{{ pre.n_items }} 路 · {{ pre.n_annotations }} 框 · ok {{ pre.n_ok }}</div>
      <div class="btns">
        <MButton size="sm" variant="ghost" :disabled="busy" @click="onSpeedup">并行加速比</MButton>
        <MButton size="sm" variant="ghost" :disabled="busy" @click="onActiveRound">主动学习一轮</MButton>
      </div>
      <KV v-if="speedup" :entries="[
        { key: '加速比', value: speedupNum + '×' },
        { key: 'workers', value: String(speedup.workers ?? workers) },
      ]" />
      <p v-if="out" class="out" role="status">{{ out }}</p>
    </div>
  </details>
</template>

<style scoped>
.smart { border-top: 1px solid var(--border); }
.smart > summary { padding: 8px var(--sp-3); font-size: var(--fs-ui); color: var(--text-secondary); cursor: pointer; list-style: none; }
.smart > summary::-webkit-details-marker { display: none; }
.smart > summary::before { content: '▸ '; }
.smart[open] > summary::before { content: '▾ '; }
.smart__body { padding: 0 var(--sp-3) var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-2); }
.row { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.lbl { font-size: var(--fs-xs); color: var(--text-secondary); }
.btns { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.stat { font-size: var(--fs-xs); color: var(--text-secondary); }
.out { margin: 0; font-size: var(--fs-xs); color: var(--accent); }
</style>

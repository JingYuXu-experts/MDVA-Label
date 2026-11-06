<script setup lang="ts">
import { computed, ref } from 'vue'
import { Trash2, X } from 'lucide-vue-next'
import MTag from '@/components/ui/MTag.vue'
import MButton from '@/components/ui/MButton.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MTabs from '@/components/ui/MTabs.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import SmartPanel from './SmartPanel.vue'
import { useCanvasStore, type DraftAnn } from '@/stores/canvas'
import { useCtxStore } from '@/stores/ctx'
import { useToast } from '@/stores/toast'
import * as api from '@/api/endpoints'

const store = useCanvasStore()
const ctx = useCtxStore()
const toast = useToast()

const tab = ref<'anns' | 'history'>('anns')
const log = ref<Record<string, unknown>[]>([])
const logLoading = ref(false)

const STATE_TAG: Record<string, 'success' | 'warning' | 'accent' | 'danger' | 'neutral'> = {
  draft: 'warning', submitted: 'accent', approved: 'success', rejected: 'danger',
}

const labelOpts = computed(() => {
  const set = new Set<string>(store.draftAnns.map((a) => a.label).filter(Boolean))
  return [...set].map((l) => ({ value: l, label: l }))
})

function pick(a: DraftAnn): void {
  store.select(a.localId)
  store.setMode('select')
}

function onLabelChange(a: DraftAnn, val: string): void {
  store.mutate(a.localId, { label: val })
  store.endEdit()
}

// ---- 审核区角色门禁 ----
const canReview = computed(() => ctx.role === 'reviewer' || ctx.role === 'admin')
const canSubmit = computed(() => ctx.role !== 'reviewer')
const reviewDialog = ref(false)
const bulkMaxConf = ref(1)

async function loadHistory(): Promise<void> {
  if (!ctx.version) return
  logLoading.value = true
  try {
    const resp = await api.getReviewLog(ctx.version)
    log.value = resp.log
  } catch (e) {
    toast.danger(`历史读取失败：${e instanceof Error ? e.message : String(e)}`)
  } finally { logLoading.value = false }
}
function switchTab(t: 'anns' | 'history'): void {
  tab.value = t
  if (t === 'history') loadHistory()
}

async function approveOne(a: DraftAnn): Promise<void> { await store.reviewOne(a, 'approve') }
async function rejectOne(a: DraftAnn): Promise<void> { await store.reviewOne(a, 'reject') }
function openBulk(action: 'approve' | 'reject'): void {
  if (!canReview.value) { toast.warning('需要 reviewer/admin 角色（后端 403 兜底）'); return }
  bulkAction.value = action
  reviewDialog.value = true
}
const bulkAction = ref<'approve' | 'reject'>('approve')
async function confirmBulk(): Promise<void> {
  await store.reviewBulk(bulkAction.value, bulkMaxConf.value < 1 ? bulkMaxConf.value : null)
  reviewDialog.value = false
}

const KEYMAP: [string, string][] = [
  ['1–6', '切模式'], ['Ctrl+S', '保存'], ['Ctrl+Z / Ctrl+Shift+Z', '撤销/重做'],
  ['← / →', '上/下一条'], ['+ / −', '缩放画布'], ['0', '适配窗口'],
  ['Delete', '删除选中'], ['Enter / 双击', '闭合多边形'], ['Esc', '取消绘制/退出选中'],
]
</script>

<template>
  <div class="panel">
    <MTabs :model-value="tab" :tabs="[{ value: 'anns', label: '标注列表' }, { value: 'history', label: '历史' }]"
      @update:model-value="switchTab($event as 'anns' | 'history')" />

    <div v-if="tab === 'anns'" class="panel__scroll">
      <div v-if="!store.draftAnns.length" class="panel__empty">
        开始绘制第一个框，或展开智能标注
      </div>
      <div
        v-for="(a, idx) in store.draftAnns"
        :key="a.localId"
        class="annrow"
        :class="{ 'is-sel': a.localId === store.selId }"
        @click="pick(a)"
      >
        <span class="annrow__chip" :style="{ background: STATE_TAG[a.state] ? undefined : 'var(--text-muted)' }">{{ idx + 1 }}</span>
        <span class="annrow__name mono">{{ a.geom }}#{{ a.localId }}</span>
        <MTag :kind="STATE_TAG[a.state] || 'neutral'" :text="a.state" />
        <span v-if="a.source !== 'manual'" class="annrow__src">{{ a.source === 'preannotate' || a.source === 'visual_prompt' || a.source === 'active_learning' ? '机' : a.source }}</span>
        <span class="annrow__spacer" />
        <MSelect :model-value="a.label" :options="labelOpts" width="90px" @click.stop
          @update:model-value="onLabelChange(a, String($event))" />
        <button v-if="canReview && a.id && a.state === 'submitted'" class="annrow__act" title="通过" @click.stop="approveOne(a)">✓</button>
        <button v-if="canReview && a.id && a.state === 'submitted'" class="annrow__act" title="驳回" @click.stop="rejectOne(a)">✕</button>
        <button class="annrow__del" title="删除（可撤销）" @click.stop="store.deleteAnn(a.localId)">
          <Trash2 :size="13" />
        </button>
      </div>
    </div>

    <div v-else class="panel__scroll">
      <div v-if="logLoading" class="panel__empty">加载中…</div>
      <div v-else-if="!log.length" class="panel__empty">暂无审核记录</div>
      <ul v-else class="log">
        <li v-for="(r, i) in log" :key="i" class="log__row">
          <span class="mono log__t">{{ r.time ?? r.created ?? '' }}</span>
          <MTag kind="neutral" :text="String(r.role ?? '')" />
          <MTag :kind="STATE_TAG[String(r.action ?? r.state ?? '')] || 'neutral'" :text="String(r.action ?? r.state ?? '')" />
          <span class="log__why">{{ r.reason || '' }}</span>
        </li>
      </ul>
    </div>

    <!-- 审核区（角色决定可用性） -->
    <div class="review">
      <MButton size="sm" variant="secondary" :disabled="!canSubmit || !store.draftAnns.length"
        :title="canSubmit ? '整条提交审核' : 'reviewer 不能提交，请切换角色/管理员'"
        @click="store.submitItem()">提交审核</MButton>
      <MButton size="sm" variant="primary" :disabled="!canReview" title="需要 reviewer/admin 角色（后端 403 兜底）"
        @click="openBulk('approve')">批量验收</MButton>
      <MButton size="sm" variant="danger" :disabled="!canReview" title="需要 reviewer/admin 角色"
        @click="openBulk('reject')">批量驳回</MButton>
    </div>

    <SmartPanel />

    <details class="keys">
      <summary>? 快捷键</summary>
      <table class="keys__tbl">
        <tr v-for="[k, d] in KEYMAP" :key="k"><td class="mono">{{ k }}</td><td>{{ d }}</td></tr>
      </table>
    </details>

    <MDialog :open="reviewDialog" title="批量审核" :width="480" @close="reviewDialog = false">
      <p class="bulk__desc">
        将对当前版本所有 {{ bulkAction === 'approve' ? 'submitted' : 'submitted' }} 条目执行
        <strong>{{ bulkAction }}</strong>（显式传 action，不吃服务端默认）。
      </p>
      <div class="bulk__conf">
        <span class="lbl">max_confidence（仅收机器预标注，可留空）</span>
        <NumberInput v-model="bulkMaxConf" :min="0" :max="1" :step="0.05" />
      </div>
      <template #footer>
        <MButton variant="ghost" @click="reviewDialog = false"><X :size="14" /> 取消</MButton>
        <MButton :variant="bulkAction === 'reject' ? 'danger' : 'primary'" @click="confirmBulk">确认</MButton>
      </template>
    </MDialog>
  </div>
</template>

<style scoped>
.panel { width: 280px; flex: none; background: var(--bg-panel); border-left: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; }
.panel__scroll { flex: 1; overflow-y: auto; }
.panel__empty { padding: var(--sp-4); font-size: var(--fs-ui); color: var(--text-muted); text-align: center; }
.annrow { display: flex; align-items: center; gap: 6px; padding: 6px var(--sp-3); cursor: pointer; border-left: 2px solid transparent; }
.annrow.is-sel { background: var(--bg-hover); border-left-color: var(--accent); }
.annrow__chip { width: 16px; height: 16px; border-radius: 3px; background: var(--bg-raised); color: var(--text-secondary); font-size: 10px; display: flex; align-items: center; justify-content: center; flex: none; }
.annrow__name { font-size: var(--fs-xs); color: var(--text-primary); }
.annrow__src { font-size: 10px; color: var(--warning); }
.annrow__spacer { flex: 1; }
.annrow__act { background: none; border: 1px solid var(--border); border-radius: 3px; color: var(--success); cursor: pointer; width: 20px; height: 20px; }
.annrow__del { background: none; border: none; color: var(--text-muted); cursor: pointer; display: flex; }
.annrow__del:hover { color: var(--danger); }
.log { list-style: none; margin: 0; padding: var(--sp-2) var(--sp-3); display: flex; flex-direction: column; gap: 6px; }
.log__row { display: flex; align-items: center; gap: 6px; font-size: var(--fs-xs); }
.log__t { color: var(--text-secondary); }
.log__why { color: var(--text-muted); }
.review { display: flex; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-top: 1px solid var(--border); flex-wrap: wrap; }
.keys { border-top: 1px solid var(--border); }
.keys > summary { padding: 6px var(--sp-3); font-size: var(--fs-xs); color: var(--text-secondary); cursor: pointer; list-style: none; }
.keys__tbl { width: 100%; border-collapse: collapse; font-size: var(--fs-xs); }
.keys__tbl td { padding: 2px var(--sp-3); color: var(--text-muted); }
.keys__tbl td:first-child { color: var(--text-secondary); width: 45%; }
.bulk__desc { font-size: var(--fs-ui); color: var(--text-secondary); margin: 0 0 var(--sp-3); }
.bulk__conf { display: flex; flex-direction: column; gap: var(--sp-1); }
.lbl { font-size: var(--fs-xs); color: var(--text-secondary); }
</style>

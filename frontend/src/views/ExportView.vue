<script setup lang="ts">
import WorkflowSteps from "@/components/layout/WorkflowSteps.vue"
/**
 * V5 导出 `/export`（UI规格 §5.5 rev7）：单列表单流，四个 Step 纵向排列（非向导弹窗）。
 * ① 格式 ② 划分 ③ 选项 ④ 动作（导出 / 回读校验 / 历史报告）。
 *
 * 导出进行中离开页面要拦一下：后端这次会执行完并落库（契约 §4 同步长任务），
 * 拦的是"你别错过结果"，不是"离开能中止导出"——文案必须说清真话。
 */
import { onBeforeRouteLeave } from 'vue-router'
import type { NavigationGuardNext } from 'vue-router'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import StepBlock from '@/components/export/StepBlock.vue'
import FormatCards from '@/components/export/FormatCards.vue'
import ParamsForm from '@/components/export/ParamsForm.vue'
import SplitSliders from '@/components/export/SplitSliders.vue'
import OptionsRow from '@/components/export/OptionsRow.vue'
import ActionBar from '@/components/export/ActionBar.vue'
import ReportList from '@/components/export/ReportList.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MButton from '@/components/ui/MButton.vue'
import { useExportStore } from '@/stores/export'
import { useCtxStore } from '@/stores/ctx'
import * as api from '@/api/endpoints'

const store = useExportStore()
const ctx = useCtxStore()

const leavePrompt = ref(false)
/**
 * vue-router 的 next 只能被消费一次：先 next(false) 再补一次 next(true) 是不生效的，
 * "仍要离开"会留在原页——所以守卫挂起不改导航，等对话框给出真实意图后再放行。
 */
let pendingNext: NavigationGuardNext | null = null

const coverage=ref<{total:number;unlabeled:number}|null>(null)
let coverageSeq=0
const skipped = computed(() => (store.result?.skipped || []).length)

onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  await store.loadFormats()
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

function onBeforeUnload(e: BeforeUnloadEvent): void {
  if (store.running) { e.preventDefault(); e.returnValue = '' }
}

onBeforeRouteLeave((_to, _from, next) => {
  if (!store.running) { next(true); return }
  pendingNext = next
  leavePrompt.value = true
})

function stay() {
  leavePrompt.value = false
  pendingNext?.(false)
  pendingNext = null
}
function leaveAnyway() {
  leavePrompt.value = false
  pendingNext?.(true)
  pendingNext = null
}

watch(() => [ctx.dataset, ctx.version], async () => {
  const n=++coverageSeq;coverage.value=null
  store.reset()
  void store.loadReports(ctx.version)
  if(ctx.dataset&&ctx.version){try{const result=await api.getItems(ctx.dataset,ctx.version,{page:1,page_size:1});if(n===coverageSeq)coverage.value={total:result.total,unlabeled:result.counts.unlabeled||0}}catch{/* Export can still show its own errors. */}}
}, { immediate: true })
</script>

<template>
  <div
    class="export"
    data-el="export-view"
    :data-format="store.chosen"
    :data-running="String(store.running)"
    :data-validated="String(store.validated?.ok ?? '')"
    :data-split-sum="String(store.splitSum)"
  >
    <WorkflowSteps /><div class="export__scroll"><div v-if="coverage" class="export-coverage"><b>当前版本 {{coverage.total.toLocaleString()}} 张图片，{{coverage.unlabeled.toLocaleString()}} 张尚无人工标注。</b><p>实际导出范围取决于标注来源、审核状态和无标注图片选项，请核对导出报告。尚未标注不代表没有目标，训练前应确认这些图片的用途。</p></div>
      <StepBlock
        no="①"
        title="格式"
        note="选择需要的训练格式；原始掩膜请使用 COCO，并在标注来源中明确选择。"
        :done="!!store.chosen"
        :warn="store.chosen ? '' : '未选格式，导出不可用'"
        tone="neutral"
      >
        <FormatCards />
        <ParamsForm :skip="[store.splitKey, store.seedKey].filter(Boolean)" />
      </StepBlock>

      <StepBlock
        no="②"
        title="划分"
        note="设置训练、验证和测试比例，总和应为100%。同源图片应放在同一划分中。"
        :done="store.splitValid"
        :warn="store.splitError"
        :tone="store.splitError ? 'danger' : 'warning'"
      >
        <SplitSliders />
      </StepBlock>

      <StepBlock
        no="③"
        title="选项"
        note="选择附加内容。导出完成后会显示保存位置和文件报告。"
        :done="true"
      >
        <OptionsRow />
      </StepBlock>

      <StepBlock
        no="④"
        title="导出与回读校验"
        note="导出后可验证文件与标注是否完整；被跳过的项目会在报告中说明原因。"
        :done="!!store.result"
        :warn="skipped ? `本次有 ${skipped} 条标注被明确跳过（原因见报告）` : ''"
      >
        <ActionBar />
        <ReportList />
      </StepBlock>
    </div>

    <MDialog :open="leavePrompt" title="导出还在进行，确定离开？" @close="stay">
      <p class="dlg__line">
        离开页面<b>不会中止导出</b>：这是同步长请求，后端会把这次执行完并落库、在任务表留痕。
        你只是看不到结果——回来时"导出历史"里能找到它。
      </p>
      <template #footer>
        <MButton variant="ghost" @click="leaveAnyway">仍要离开</MButton>
        <MButton variant="primary" @click="stay">留下等待</MButton>
      </template>
    </MDialog>
  </div>
</template>

<style scoped>
.export { flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--bg-base); }
.export__scroll { flex: 1; min-height: 0; overflow-y: auto; }
.dlg__line { margin: 0; font-size: var(--fs-md); line-height: 1.6; }
.dlg__line b { color: var(--accent); }
.export-coverage{padding:16px 24px;border-bottom:1px solid var(--border);font-size:12px;background:var(--bg-panel)}.export-coverage p{font-size:11px;line-height:1.8;color:var(--text-muted);margin-bottom:0}
</style>

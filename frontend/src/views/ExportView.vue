<script setup lang="ts">
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

const store = useExportStore()
const ctx = useCtxStore()

const leavePrompt = ref(false)
/**
 * vue-router 的 next 只能被消费一次：先 next(false) 再补一次 next(true) 是不生效的，
 * "仍要离开"会留在原页——所以守卫挂起不改导航，等对话框给出真实意图后再放行。
 */
let pendingNext: NavigationGuardNext | null = null

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

watch(() => [ctx.dataset, ctx.version], () => {
  store.reset()
  void store.loadReports(ctx.version)
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
    <div class="export__scroll">
      <StepBlock
        no="①"
        title="格式"
        note="卡片与参数都来自 GET /api/export/formats 的元数据：后端加一种格式，界面自动多一张卡。"
        :done="!!store.chosen"
        :warn="store.chosen ? '' : '未选格式，导出不可用'"
      >
        <FormatCards />
        <ParamsForm :skip="[store.splitKey, store.seedKey].filter(Boolean)" />
      </StepBlock>

      <StepBlock
        no="②"
        title="划分"
        note="train/val/test 三段比例；和不为 100% 时前端直接拦下，不发请求。"
        :done="store.splitValid"
        :warn="store.splitError"
      >
        <SplitSliders />
      </StepBlock>

      <StepBlock
        no="③"
        title="选项"
        note="训练脚本开关与落盘位置。落盘位置由后端决定并回显，界面只读不猜。"
        :done="true"
      >
        <OptionsRow />
      </StepBlock>

      <StepBlock
        no="④"
        title="导出与回读校验"
        note="导出是同步长请求；成功后建议立即回读校验——产物与版本对不对，只有对账才算数。"
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
</style>

<script setup lang="ts">
/**
 * V6 后端（算法替换）`/backends`（UI规格 v1.4 §5.6）。
 *
 * 布局按条文：左 300px 是筛选 + 后端清单，右内容区四个 Tab（清单详情 / 推荐 / 门槛 / 实测）。
 * 三条实现口径：
 * 1) 一切枚举来自元数据——后端名、domain/task/modality 候选、参数表都不写死；
 * 2) 空态先行：没有实测表时推荐与门槛呈现设计空态（含引导动作），清单与筛选照常可用；
 * 3) 实测是同步分钟级长请求：loading + indeterminate + 离开确认，取消不谎称已停止。
 */
import { onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import type { NavigationGuardNext } from 'vue-router'
import MTabs from '@/components/ui/MTabs.vue'
import MDialog from '@/components/ui/MDialog.vue'
import MButton from '@/components/ui/MButton.vue'
import ProbeBar from '@/components/backends/ProbeBar.vue'
import FilterPanel from '@/components/backends/FilterPanel.vue'
import BackendList from '@/components/backends/BackendList.vue'
import InfoTab from '@/components/backends/InfoTab.vue'
import RecommendTab from '@/components/backends/RecommendTab.vue'
import GateTab from '@/components/backends/GateTab.vue'
import BenchTab from '@/components/backends/BenchTab.vue'
import { useBackendsStore } from '@/stores/backends'

const store = useBackendsStore()
const tab = ref('info')
const tabs = [
  { value: 'info', label: '清单详情' },
  { value: 'reco', label: '推荐' },
  { value: 'gate', label: '门槛' },
  { value: 'bench', label: '实测' },
]

onMounted(async () => {
  await store.loadCatalog()
  await Promise.all([store.loadBench(), store.loadRecommend(), store.loadGate()])
})

/** 筛选变了推荐就得重算（门槛同理），不需要用户刷新页面 */
watch(() => [store.domain, store.task], () => {
  void store.loadRecommend()
  void store.loadGate()
})

// ---- 实测进行中的离开拦截（§3.2-5）：next() 必须等用户选完再放行 ----
const leavePrompt = ref(false)
let pendingNext: NavigationGuardNext | null = null
onBeforeRouteLeave((_to, _from, next) => {
  if (!store.benching) { next(true); return }
  pendingNext = next
  leavePrompt.value = true
})
function stay(): void {
  leavePrompt.value = false
  pendingNext?.(false)
  pendingNext = null
}
function leaveAnyway(): void {
  leavePrompt.value = false
  store.cancelBench()
  pendingNext?.(true)
  pendingNext = null
}
</script>

<template>
  <div class="bk" data-testid="backends-view">
    <ProbeBar />
    <div class="bk__main">
      <aside class="bk__rail">
        <FilterPanel />
        <BackendList />
      </aside>
      <section class="bk__detail">
        <MTabs v-model="tab" :tabs="tabs" aria-label="后端详情分页" />
        <div class="bk__panel" :data-tab="tab">
          <InfoTab v-if="tab === 'info'" :row="store.chosenRow" />
          <RecommendTab v-else-if="tab === 'reco'" @goto-bench="tab = 'bench'" />
          <GateTab v-else-if="tab === 'gate'" />
          <BenchTab v-else />
        </div>
      </section>
    </div>

    <MDialog :open="leavePrompt" title="实测还在跑" :width="480" @close="stay">
      <p class="bk__dlg">
        实测是同步长请求。离开本页只是不再等待结果，后端这次评测会继续跑完并落表；
        你可以稍后在任务清单核对结果。要现在离开吗？
      </p>
      <template #footer>
        <MButton variant="secondary" @click="stay">继续等待</MButton>
        <MButton variant="danger" @click="leaveAnyway">离开本页</MButton>
      </template>
    </MDialog>
  </div>
</template>

<style scoped>
.bk { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.bk__main { display: flex; flex: 1; min-height: 0; }
.bk__rail { width: 300px; flex: none; display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border); background: var(--bg-base); }
.bk__detail { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.bk__panel { flex: 1; min-height: 0; overflow-y: auto; }
.bk__dlg { margin: 0; font-size: var(--fs-ui); color: var(--text-secondary); line-height: 1.65; }
</style>

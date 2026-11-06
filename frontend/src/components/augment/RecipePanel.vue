<script setup lang="ts">
/**
 * 左 300px 配方面板（§5.4）：Tabs=学科预设 / 推荐 / 自定义算子表，
 * 三个 Tab 的数据全部来自 GET /api/augment/recipes（ops + presets + task_presets），
 * 前端不写死学科清单、算子清单或参数上界。
 * 配方层参数（seed/factor/limit/index）常驻底部：seed 是可复现的前提，界面必须显示实际值。
 */
import { computed } from 'vue'
import MTabs from '@/components/ui/MTabs.vue'
import MButton from '@/components/ui/MButton.vue'
import MTag from '@/components/ui/MTag.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import PresetCard from './PresetCard.vue'
import OpRow from './OpRow.vue'
import { useAugmentStore } from '@/stores/augment'
import { useCtxStore } from '@/stores/ctx'

const store = useAugmentStore()
const ctx = useCtxStore()

const tabs = [
  { value: 'preset', label: '学科预设' },
  { value: 'suggest', label: '推荐' },
  { value: 'custom', label: '自定义算子' },
]

const datasetType = computed(() => ctx.currentDataset?.type || '')
/** task_presets：数据集任务类型 → 默认预设名，用来标出"开箱即用"的那一张卡 */
const defaultPresetName = computed(() => store.taskPresets[datasetType.value] || '')
const pad = (n: number) => String(n).padStart(2, '0')
const checkedAtText = computed(() => {
  if (!store.checkedAt) return ''
  const d = new Date(store.checkedAt)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
})
const suggestPresetName = computed(() => store.suggested?.preset?.name || '')

function apply(name: string) {
  const p = store.presets.find(x => x.name === name)
  if (p) store.applyPreset(p)
}
</script>

<template>
  <aside
    class="recipe"
    aria-label="增强配方面板"
    :data-tab="store.tab"
    :data-presets="store.presets.map(p => p.name).join(',')"
    :data-ops="store.ops.map(o => o.name).join(',')"
    :data-active-preset="store.sourceName"
    :data-edited="String(store.edited)"
    :data-seed="String(store.seed)"
    :data-factor="String(store.factor)"
    :data-check-ran="String(store.checkRan)"
    :data-warn-count="String(store.warnings.length)"
  >
    <MTabs v-model="store.tab" :tabs="tabs" aria-label="配方来源" />

    <MSkeleton v-if="store.metaLoading" :show="true" variant="rows" :count="4" />
    <p v-else-if="store.metaError" class="recipe__err" role="alert">配方元数据读不出来：{{ store.metaError }}</p>

    <div v-else class="recipe__scroll">
      <!-- 学科预设 -->
      <template v-if="store.tab === 'preset'">
        <p class="recipe__lead">
          选一张卡「填入配方」即可执行；<b>reason 是后端原文</b>，说清这套参数为什么这么配。
        </p>
        <PresetCard
          v-for="p in store.presets"
          :key="p.name"
          :preset="p"
          :active="store.sourceName === p.name && !store.edited"
          :default-for-task="defaultPresetName === p.name"
          @apply="apply(p.name)"
        />
        <p v-if="datasetType" class="recipe__note mono">
          task_presets[{{ datasetType }}] = {{ defaultPresetName || '（无映射，回落 cv_segmentation）' }}
        </p>
      </template>

      <!-- 推荐 -->
      <template v-else-if="store.tab === 'suggest'">
        <p class="recipe__lead">
          推荐来自 <code>POST /api/augment/suggest</code>：先看数据集学科，再看模态分布，最后看任务类型。
        </p>
        <MButton
          variant="primary"
          :loading="store.suggesting"
          :disabled="!ctx.dataset"
          title="按当前数据集挑一个可直接用的配方"
          @click="store.runSuggest(ctx.dataset)"
        >
          一键增强推荐
        </MButton>
        <p v-if="store.suggestError" class="recipe__err" role="alert">{{ store.suggestError }}</p>
        <PresetCard
          v-if="store.suggested"
          class="recipe__suggested"
          :preset="store.suggested.preset"
          :active="store.sourceName === suggestPresetName && !store.edited"
          @apply="apply(suggestPresetName)"
        />
        <MEmpty v-else-if="!store.suggesting" text="点上方按钮取本数据集的推荐配方" />
      </template>

      <!-- 自定义算子表 -->
      <template v-else>
        <p class="recipe__lead">
          算子表与参数上下界都来自后端元数据（勾选即进链，链顺序按算子表顺序）。
        </p>
        <OpRow
          v-for="o in store.ops"
          :key="o.name"
          :meta="o"
          :state="store.state[o.name]"
          @toggle="store.toggleOp(o.name, $event)"
          @param="(k, v) => store.setParam(o.name, k, v)"
        />
      </template>
    </div>

    <div class="recipe__level">
      <h3 class="recipe__h">配方参数</h3>
      <div class="recipe__seed">
        <NumberInput
          :model-value="store.seed"
          :min="0"
          :step="1"
          label="seed（随机种子）"
          aria-label="增强随机种子"
          @update:model-value="store.setSeed(Number($event))"
        />
        <MButton variant="ghost" size="sm" title="换一个随机 seed（界面显示的就是实际值）" @click="store.reseed()">
          随机
        </MButton>
      </div>
      <p class="recipe__note">
        seed 显示的就是要发后端的实际值——谱系里存的也是它，可复现校验靠这个数。
      </p>
      <div class="recipe__grid">
        <NumberInput
          :model-value="store.factor"
          :min="1"
          :step="1"
          label="factor 每张副本数"
          aria-label="每张原件产生的副本数"
          @update:model-value="store.setFactor(Number($event))"
        />
        <NumberInput
          :model-value="store.limit ?? 0"
          :min="0"
          :step="1"
          label="limit 最多处理（0=全部）"
          aria-label="最多处理多少条原件"
          @update:model-value="store.setLimit(Number($event) > 0 ? Number($event) : null)"
        />
        <NumberInput
          :model-value="store.sliceIndex"
          :min="0"
          :step="1"
          label="index 体/视频取哪刀"
          aria-label="体数据或视频取帧序号"
          @update:model-value="store.setSliceIndex(Number($event))"
        />
      </div>
      <div class="recipe__check">
        <MTag :kind="store.checkRan ? (store.warnings.length ? 'warning' : 'success') : 'neutral'"
          :text="store.checkRan ? (store.warnings.length ? `${store.warnings.length} 条告警` : '无告警') : '未做告警检查'"
        />
        <span v-if="store.checking" class="recipe__checkstate" role="status">检查中…</span>
        <span v-else-if="checkedAtText" class="recipe__checkstate">检查于 {{ checkedAtText }}</span>
      </div>
      <p class="recipe__note">
        改任何参数都会自动重跑告警检查（500ms 防抖），不需要手动点检查。
      </p>
    </div>
  </aside>
</template>

<style scoped>
.recipe {
  width: 300px; flex: 0 0 300px; display: flex; flex-direction: column; min-height: 0;
  background: var(--bg-panel); border-right: 1px solid var(--border);
}
.recipe__scroll { flex: 1; min-height: 0; overflow-y: auto; padding: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-3); }
.recipe__lead { margin: 0; font-size: var(--fs-xs); line-height: 1.55; color: var(--text-secondary); }
.recipe__lead b { color: var(--text-primary); }
.recipe__lead code { font-family: var(--font-mono); color: var(--accent); }
.recipe__err { margin: 0; font-size: var(--fs-xs); color: var(--danger); word-break: break-all; }
.recipe__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.5; }
.recipe__suggested { border-color: var(--accent); }
.recipe__level {
  display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3);
  border-top: 1px solid var(--border); background: var(--bg-raised);
}
.recipe__h { margin: 0; font-size: var(--fs-md); color: var(--text-primary); }
.recipe__seed { display: flex; align-items: flex-end; gap: var(--sp-2); }
.recipe__grid { display: flex; flex-direction: column; gap: var(--sp-2); }
.recipe__check { display: flex; align-items: center; gap: var(--sp-2); }
.recipe__checkstate { font-size: var(--fs-xs); color: var(--text-muted); }
</style>

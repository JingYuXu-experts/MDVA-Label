<script setup lang="ts">
/**
 * 划分（§5.5 Step②）：三段比例滑杆 + 合计提示 + 划分种子。
 *
 * 非法值（和≠100%）在前端就拦下、不发请求（验收 2）：后端会把比例归一，
 * 若界面放行非法值，用户以为导的是 60/20/20，实际拿到的是归一后的另一套折分。
 * 真实落进各折的条数只显示后端回的 split 摘要，前端不预测。
 */
import { computed } from 'vue'
import MSlider from '@/components/ui/MSlider.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import MTag from '@/components/ui/MTag.vue'
import { useExportStore } from '@/stores/export'

const store = useExportStore()
/** 将要上送的比例串：界面显示的就是这个值，不做二次加工 */
const ratioStr = computed(() => String(store.payload[store.splitKey] ?? ''))
</script>

<template>
  <div
    class="split"
    :data-sum="String(store.splitSum)"
    :data-valid="String(store.splitValid)"
    :data-ratio="ratioStr"
    :data-has-split="String(store.hasSplit)"
  >
    <template v-if="store.hasSplit">
      <div v-for="(f, i) in store.folds" :key="f" class="split__row">
        <span class="split__name num">{{ f }}</span>
        <MSlider
          :model-value="store.split[i]"
          :min="0"
          :max="100"
          :step="1"
          unit="%"
          :aria-label="`${store.chosen} ${f} 占比`"
          @update:model-value="store.setSplit(i, Number($event))"
        />
        <span class="split__pct num">{{ store.split[i] }}%</span>
      </div>

      <p
        class="split__sum"
        :class="store.splitValid ? 'is-ok' : 'is-bad'"
        :role="store.splitValid ? 'status' : 'alert'"
      >
        <template v-if="store.splitValid">合计 100%，将按 <code class="mono">{{ ratioStr }}</code> 分层划分</template>
        <template v-else>{{ store.splitError }}</template>
      </p>

      <NumberInput
        v-if="store.seedKey"
        :model-value="store.splitSeed"
        :min="0"
        :step="1"
        label="划分随机种子（seed）"
        aria-label="划分随机种子"
        @update:model-value="store.setSeed(Number($event))"
      />
      <p class="split__note">
        分层键是"每张图出现最多的类别"，同组（同源序列/同一场景）不拆到不同折——这两条都由后端 split.plan_split 决定。
      </p>
    </template>

    <p v-else class="split__none">
      <MTag kind="neutral" text="无划分参数" />
      这种格式的参数表里没有"三段比例串"参数，划分不走界面下发：引擎按默认 0.7/0.15/0.15 分层写 splits 文件。
      导出后下面会显示后端实际写出的折数。
    </p>

    <div v-if="store.splitOutcome" class="split__actual">
      <h3 class="split__h3">上次导出实际落进各折（后端 split 摘要）</h3>
      <ul class="split__list">
        <li v-for="f in store.folds" :key="f" class="split__item num">
          {{ f }} {{ store.splitOutcome.counts[f] ?? 0 }} 张
          <span class="split__ratio">({{ ((store.splitOutcome.ratios[f] ?? 0) * 100).toFixed(1) }}%)</span>
        </li>
      </ul>
      <p class="split__note">合计 {{ store.splitOutcome.n }} 张样本，与上方滑杆的比例不冲突——这是真实结果，不是预测。</p>
    </div>
  </div>
</template>

<style scoped>
.split { display: flex; flex-direction: column; gap: var(--sp-3); max-width: 620px; }
.split__row { display: grid; grid-template-columns: 72px 1fr 56px; align-items: center; gap: var(--sp-3); }
.split__name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-secondary); }
.split__pct { font-family: var(--font-mono); font-size: var(--fs-md); color: var(--text-primary); text-align: right; }
.split__sum { margin: 0; font-size: var(--fs-xs); line-height: 1.5; }
.split__sum.is-ok { color: var(--text-secondary); }
.split__sum.is-bad { color: var(--danger); font-weight: 600; }
.split__sum code { color: var(--accent); }
.split__note { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.55; }
.split__none {
  margin: 0; display: flex; align-items: flex-start; gap: var(--sp-2); font-size: var(--fs-xs);
  color: var(--text-secondary); line-height: 1.6;
}
.split__actual { display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-raised); }
.split__h3 { margin: 0; font-size: var(--fs-md); color: var(--text-primary); }
.split__list { list-style: none; margin: 0; padding: 0; display: flex; gap: var(--sp-4); flex-wrap: wrap; }
.split__item { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-secondary); }
.split__ratio { color: var(--text-muted); }
</style>

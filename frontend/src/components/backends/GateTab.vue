<script setup lang="ts">
import { watch } from 'vue'
import MTag from '@/components/ui/MTag.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import { useBackendsStore } from '@/stores/backends'

/**
 * 门槛（§5.6）：判定来自 GET /api/gate，界面只把 f1 与 threshold 的差距画出来。
 * 没有实测数据时是中性"无数据"，不得出绿灯也不得出红灯——
 * "还没测过"既不是达标也不是不达标。
 */
const store = useBackendsStore()

watch(() => store.threshold, () => { void store.loadGate() })
</script>

<template>
  <div class="gate" data-testid="gate-tab">
    <div class="gate__ctl">
      <NumberInput :model-value="store.threshold" label="F1 门槛" :min="0.1" :max="1" :step="0.01"
        @update:model-value="store.threshold = $event" />
      <span class="gate__who mono">{{ store.chosen || '未选后端' }}</span>
    </div>

    <MEmpty v-if="!store.chosen" text="左侧清单里选一个后端，再看它能不能过门槛。" />
    <template v-else-if="store.gate">
      <div class="gate__verdict"
        :data-usable="store.gate.usable ? '1' : '0'"
        :data-measured="store.gateMeasured ? '1' : '0'">
        <MTag v-if="!store.gateMeasured" kind="neutral" text="无数据" />
        <MTag v-else-if="store.gate.usable" kind="success" text="达标" />
        <MTag v-else kind="danger" text="未达标" />
        <span class="gate__num num" :data-f1="store.gate.f1 ?? ''" :data-threshold="store.gate.threshold">
          实测 F1 {{ store.gate.f1 === null ? '—' : store.gate.f1.toFixed(3) }} · 门槛
          {{ store.gate.threshold.toFixed(2) }}
          <template v-if="store.gateMeasured && store.gate.f1 !== null">
            · 差距 {{ (store.gate.f1 - store.gate.threshold >= 0 ? '+' : '') + (store.gate.f1 - store.gate.threshold).toFixed(3) }}
          </template>
        </span>
      </div>

      <div class="gauge" role="img"
        :aria-label="`实测 F1 ${store.gate.f1?.toFixed(3) ?? '无数据'}，门槛 ${store.gate.threshold.toFixed(2)}`">
        <span v-if="store.gateMeasured" class="gauge__fill" :class="{ 'is-bad': !store.gate.usable }"
          :style="{ width: `${Math.max(0, Math.min(1, store.gate.f1 ?? 0)) * 100}%` }" />
        <span v-else class="gauge__none">无实测数据</span>
        <span class="gauge__mark" :style="{ left: `${store.gate.threshold * 100}%` }" aria-hidden="true" />
      </div>

      <p class="gate__reason" data-testid="gate-reason">{{ store.gate.reason }}</p>
    </template>
    <p v-else class="gate__wait">门槛判定加载中或后端未返回。</p>
  </div>
</template>

<style scoped>
.gate { display: flex; flex-direction: column; gap: var(--sp-4); padding: var(--sp-4) var(--sp-5); }
.gate__ctl { display: flex; align-items: flex-end; gap: var(--sp-3); }
.gate__who { margin-left: auto; font-size: var(--fs-xs); color: var(--text-muted); }
.gate__verdict { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.gate__num { font-size: var(--fs-xs); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.gauge { position: relative; height: 18px; background: var(--bg-raised); border: 1px solid var(--border); border-radius: var(--radius-control); overflow: hidden; }
.gauge__fill { position: absolute; inset: 0 auto 0 0; background: var(--success); }
.gauge__fill.is-bad { background: var(--danger); }
.gauge__none { position: absolute; inset: 0; display: flex; align-items: center; padding-left: var(--sp-2); font-size: var(--fs-xs); color: var(--text-muted); }
.gauge__mark { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--accent); transform: translateX(-1px); }
.gate__reason { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.6; }
.gate__wait { margin: 0; font-size: var(--fs-xs); color: var(--text-secondary); }
</style>

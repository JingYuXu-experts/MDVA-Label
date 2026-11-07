<script setup lang="ts">
import { computed } from 'vue'
import MTag from '@/components/ui/MTag.vue'
import MSkeleton from '@/components/ui/MSkeleton.vue'
import { useBackendsStore } from '@/stores/backends'
import type { BackendRow } from '@/api/types'

/**
 * 后端清单行（§5.6）：名称 + 可用性 Tag + 实测延迟。
 * available=false 灰化并把 reason 原文挂 tooltip——三个深度占位后端是"占位实现"，
 * 不是故障，所以文案一律用后端原话，界面不自创"加载失败/异常"这类词。
 */
const store = useBackendsStore()

const rows = computed(() => store.backends)

/** 延迟取该后端在当前 task 下的最优实测行；没实测就如实说"未实测"，不填 0 */
function latencyOf(b: BackendRow): string {
  const hit = store.benchRows.filter((r) => r.backend === b.name
    && (!store.task || r.task === store.task))
  if (!hit.length) return '未实测'
  const best = hit.reduce((a, r) => (r.latency_ms < a.latency_ms ? r : a), hit[0])
  return `${best.latency_ms.toFixed(1)} ms`
}
</script>

<template>
  <div class="blist" data-testid="backend-list" :aria-busy="store.loading">
    <MSkeleton v-if="store.loading" :show="true" variant="rows" :count="6" />
    <p v-else-if="store.loadError" class="blist__err" role="alert">
      清单读取失败：{{ store.loadError }}
    </p>
    <p v-else-if="!rows.length" class="blist__empty">
      当前筛选条件下后端清单为空（domain · task · modality 三项取交集）。
    </p>
    <button v-for="b in rows" v-else :key="b.name" type="button"
      class="brow" :class="{ 'is-off': !b.available, 'is-sel': store.chosen === b.name }"
      :data-backend="b.name" :data-available="b.available ? '1' : '0'"
      :title="b.available ? (b.note || b.name) : b.reason"
      :aria-pressed="store.chosen === b.name" @click="store.select(b.name)">
      <span class="brow__rail" :data-family="b.family" aria-hidden="true" />
      <span class="brow__main">
        <span class="brow__name">{{ b.title }}</span>
        <span class="brow__id mono">{{ b.name }}</span>
      </span>
      <MTag :kind="b.available ? 'success' : 'neutral'" :text="b.available ? '可用' : '不可用'" />
      <span class="brow__lat num">{{ latencyOf(b) }}</span>
    </button>
  </div>
</template>

<style scoped>
.blist { display: flex; flex-direction: column; gap: 2px; padding: var(--sp-2) var(--sp-3); overflow-y: auto; }
.blist__err { margin: var(--sp-2) 0; font-size: var(--fs-xs); color: var(--danger); }
.blist__empty { margin: var(--sp-2) 0; font-size: var(--fs-xs); color: var(--text-secondary); }
.brow {
  display: flex; align-items: center; gap: var(--sp-2); width: 100%; text-align: left;
  padding: 6px var(--sp-2) 6px 0; background: none; border: none; border-radius: var(--radius-control);
  color: inherit; cursor: pointer; font: inherit;
}
.brow:hover { background: var(--bg-hover); }
.brow:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.brow.is-sel { background: var(--bg-raised); }
/* 不可用只降不隐藏：清单必须完整可见，原因靠 tooltip 原文 */
.brow.is-off { opacity: .55; }
.brow__rail { width: 3px; align-self: stretch; border-radius: 2px; background: var(--border-strong); flex: none; margin-right: 5px; }
.brow__rail[data-family='classic'] { background: var(--accent); }
.brow__rail[data-family='visual'] { background: var(--success); }
.brow__rail[data-family='deep_stub'] { background: repeating-linear-gradient(135deg, var(--border-strong) 0 3px, transparent 3px 6px); }
.brow__main { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1; }
.brow__name { font-size: var(--fs-ui); color: var(--text-primary); }
.brow__id { font-size: 10px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.brow__lat { font-size: var(--fs-xs); color: var(--text-secondary); font-variant-numeric: tabular-nums; min-width: 66px; text-align: right; }
</style>

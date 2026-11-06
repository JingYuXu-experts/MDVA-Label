<script setup lang="ts">
import { computed } from 'vue'
import MEmpty from '@/components/ui/MEmpty.vue'
import MTag from '@/components/ui/MTag.vue'
import { useBackendsStore } from '@/stores/backends'

/**
 * 推荐（§5.6）：排序完全来自 GET /api/recommend，界面不重排、不改分。
 * 没实测表时是设计空态——note 用后端原文，并给一个"去实测"的动作，
 * 而不是把空列表伪装成"没有候选后端"。
 */
const store = useBackendsStore()
const emit = defineEmits<{ 'goto-bench': [] }>()

const rows = computed(() => store.recommended)
const maxScore = computed(() => rows.value.reduce((m, r) => Math.max(m, r.score), 0))
</script>

<template>
  <div class="reco" data-testid="recommend-tab" :data-has-table="store.hasTable ? '1' : '0'">
    <MEmpty v-if="!store.hasTable" :text="store.recommendNote || '还没有实测表'"
      action-label="去实测" @action="emit('goto-bench')">
      <p class="reco__tip">
        清单与筛选不受影响，照常可用；推荐分需要本机实测数据才能算。
      </p>
    </MEmpty>
    <ol v-else class="reco__list">
      <li v-for="(r, i) in rows" :key="r.backend" class="rcard" :data-rank="i + 1"
        :data-backend="r.backend">
        <span class="rcard__no num">{{ i + 1 }}</span>
        <div class="rcard__body">
          <div class="rcard__head">
            <span class="rcard__title">{{ r.title }}</span>
            <span class="rcard__id mono">{{ r.backend }}</span>
            <MTag v-if="!r.measured" kind="neutral" text="未实测" />
            <MTag v-else kind="accent" :text="`F1 ${r.f1.toFixed(2)}`" />
            <span class="rcard__score num" :data-score="r.score">
              得分 {{ r.score.toFixed(3) }}
            </span>
          </div>
          <div class="rcard__bar" role="img"
            :aria-label="`得分 ${r.score.toFixed(3)}，本屏最高 ${maxScore.toFixed(3)}`">
            <span class="rcard__fill" :style="{ width: `${maxScore ? (r.score / maxScore) * 100 : 0}%` }" />
          </div>
          <ul class="rcard__why">
            <li v-for="(w, k) in r.reasons" :key="k">{{ w }}</li>
          </ul>
        </div>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.reco { padding: var(--sp-4) var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-3); }
.reco__tip { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.6; }
.reco__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-3); }
.rcard { display: flex; gap: var(--sp-3); align-items: flex-start; }
.rcard__no { flex: 0 0 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--border-strong); border-radius: 50%; font-size: var(--fs-xs); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.rcard__body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.rcard__head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.rcard__title { font-size: var(--fs-ui); color: var(--text-primary); font-weight: 600; }
.rcard__id { font-size: 10px; color: var(--text-muted); }
.rcard__score { margin-left: auto; font-size: var(--fs-xs); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.rcard__bar { height: 4px; background: var(--bg-raised); border-radius: 2px; overflow: hidden; }
.rcard__fill { display: block; height: 100%; background: var(--accent); transition: width .18s ease; }
.rcard__why { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.rcard__why li { font-size: var(--fs-xs); color: var(--text-secondary); line-height: 1.55; }
</style>

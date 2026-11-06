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
  <div class="reco" data-testid="recommend-tab" :data-has-table="store.hasTable ? '1' : '0'"
    :data-source="store.hasTable ? 'bench' : 'catalog'" :data-candidates="String(rows.length)">
    <MEmpty v-if="!rows.length" :text="store.recommendNote || '还没有实测表'"
      action-label="去实测" @action="emit('goto-bench')">
      <p class="reco__tip">
        清单与筛选不受影响，照常可用；推荐分需要本机实测数据才能算。
      </p>
    </MEmpty>
    <template v-else>
      <!-- 产品经理 seq=552 §三 recommend ②：has_table=false 时 recommended 仍可能非空——
           那是"按静态目录 + 档位给的建议先试"，不是实测排名。
           所以列表必须照常渲染（不藏进空态），但必须把来源说在明面上，且用 neutral 档
           （REQ-F14：前置条件未满足 ≠ 提醒 ≠ 失败），不得渲染成"实测结果"。
           note 有后端原文时优先显示原文；后端补原文前这条是显式声明的前端文案，不冒充接口原文。 -->
      <p v-if="!store.hasTable" class="reco__src" data-testid="recommend-source" role="note">
        {{ store.recommendNote || '本机尚无实测记录：以下候选按静态目录与档位给出，属「建议先试」，不是实测排名。' }}
        <button class="reco__go" type="button" @click="emit('goto-bench')">去实测</button>
      </p>
      <ol class="reco__list">
        <li v-for="(r, i) in rows" :key="r.backend" class="rcard" :data-rank="i + 1"
          :data-backend="r.backend" :data-measured="r.measured ? '1' : '0'">
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
    </template>
  </div>
</template>

<style scoped>
.reco { padding: var(--sp-4) var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-3); }
.reco__tip { margin: var(--sp-2) 0 0; font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.6; }
/* 来源说明属"前置条件未满足"，neutral 档：不占橙（提醒）、不占红（失败）（REQ-F14 三档通则） */
.reco__src {
  margin: 0; padding: var(--sp-2) var(--sp-3); font-size: var(--fs-xs); line-height: 1.6;
  color: var(--text-secondary); background: var(--bg-raised);
  border: 1px solid var(--border); border-radius: var(--radius-control);
}
.reco__go {
  margin-left: var(--sp-2); background: none; border: none; padding: 0; cursor: pointer;
  font: inherit; color: var(--accent); text-decoration: underline;
}
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

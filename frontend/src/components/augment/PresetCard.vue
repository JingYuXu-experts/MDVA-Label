<script setup lang="ts">
/**
 * 学科预设卡：标题 + 为什么这么配（后端 reason 原文，界面不改写）+ 算子摘要 + 副本倍数。
 * "一键增强"的落点就是这张卡：填进配方即可用，不需要用户再猜参数。
 */
import { computed } from 'vue'
import MButton from '@/components/ui/MButton.vue'
import MTag from '@/components/ui/MTag.vue'
import type { AugPreset } from '@/api/types'

const props = defineProps<{
  preset: AugPreset
  active?: boolean
  defaultForTask?: boolean
}>()
const emit = defineEmits<{ apply: [] }>()

const opNames = computed(() => (props.preset.recipe?.ops || []).map(o => o.name))
const factor = computed(() => Number(props.preset.recipe?.factor) || 1)
</script>

<template>
  <article class="pcard" :class="{ 'is-active': active }">
    <header class="pcard__head">
      <h4 class="pcard__title">{{ preset.title }}</h4>
      <MTag v-if="defaultForTask" kind="accent" text="本数据集默认" />
      <MTag v-if="active" kind="success" text="已填入" />
    </header>
    <p class="pcard__reason">{{ preset.reason }}</p>
    <p class="pcard__chain num" :title="opNames.join(' → ')">{{ opNames.join(' → ') }}</p>
    <footer class="pcard__foot">
      <span class="pcard__factor">每张原件产 <b class="num">{{ factor }}</b> 个副本</span>
      <MButton
        variant="secondary"
        size="sm"
        :aria-label="`把配方「${preset.title}」填入当前配方`"
        @click="emit('apply')"
      >
        填入配方
      </MButton>
    </footer>
  </article>
</template>

<style scoped>
.pcard {
  display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3);
  border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-raised);
}
.pcard.is-active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-soft); }
.pcard__head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.pcard__title { margin: 0; font-size: var(--fs-md); color: var(--text-primary); font-weight: 600; }
.pcard__reason { margin: 0; font-size: var(--fs-xs); line-height: 1.55; color: var(--text-secondary); }
.pcard__chain {
  margin: 0; font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pcard__foot { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.pcard__factor { font-size: var(--fs-xs); color: var(--text-muted); }
.pcard__factor b { color: var(--text-primary); font-family: var(--font-mono); }
</style>

<script setup lang="ts" generic="T extends string">
export interface SegmentOption<V = string> { value: V; label: string; icon?: unknown; title?: string }

withDefaults(defineProps<{
  modelValue: T
  options: SegmentOption<T>[]
  ariaLabel?: string
}>(), { ariaLabel: '分段选择' })

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
</script>

<template>
  <div class="segmented" role="radiogroup" :aria-label="ariaLabel">
    <button v-for="option in options" :key="String(option.value)" type="button"
      class="segmented__item" :class="{ 'is-active': option.value === modelValue }"
      role="radio" :aria-checked="option.value === modelValue" :title="option.title || option.label"
      @click="emit('update:modelValue', option.value)">
      <component :is="option.icon" v-if="option.icon" :size="14" aria-hidden="true" />
      <span>{{ option.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.segmented {
  display: inline-flex; align-items: center;
  background: var(--bg-raised); border: 1px solid var(--border);
  border-radius: var(--radius-control); padding: 1px; gap: 1px;
}
.segmented__item {
  display: inline-flex; align-items: center; gap: var(--sp-1);
  height: 24px; padding: 0 var(--sp-2);
  background: transparent; border: none; border-radius: 2px;
  color: var(--text-secondary); font-size: var(--fs-ui);
  cursor: pointer; white-space: nowrap;
}
.segmented__item:hover { background: var(--bg-hover); color: var(--text-primary); }
.segmented__item.is-active { background: var(--accent); color: #FFFFFF; }
.segmented__item.is-active:hover { background: var(--accent-hover); }
</style>

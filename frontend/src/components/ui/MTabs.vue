<script setup lang="ts">
withDefaults(defineProps<{
  modelValue: string
  tabs: { value: string; label: string }[]
  ariaLabel?: string
}>(), { ariaLabel: '分页签' })

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <div class="tabs" role="tablist" :aria-label="ariaLabel">
    <button v-for="tab in tabs" :key="tab.value" type="button" role="tab"
      class="tabs__item" :class="{ 'is-active': tab.value === modelValue }"
      :aria-selected="tab.value === modelValue"
      @click="emit('update:modelValue', tab.value)">
      {{ tab.label }}
    </button>
  </div>
</template>

<style scoped>
.tabs {
  display: flex; align-items: stretch; gap: var(--sp-3);
  border-bottom: 1px solid var(--border);
}
.tabs__item {
  background: transparent; border: none; border-bottom: 2px solid transparent;
  padding: var(--sp-1) 0; margin-bottom: -1px;
  color: var(--text-secondary); font-size: var(--fs-ui); cursor: pointer;
}
.tabs__item:hover { color: var(--text-primary); }
.tabs__item.is-active { color: var(--accent); border-bottom-color: var(--accent); }
</style>

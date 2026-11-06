<script setup lang="ts">
withDefaults(defineProps<{
  modelValue: boolean
  label?: string
  ariaLabel?: string
  disabled?: boolean
}>(), { modelValue: false, label: '', ariaLabel: '', disabled: false })

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <label class="checkbox" :class="{ 'is-disabled': disabled }">
    <input type="checkbox" :checked="modelValue" :disabled="disabled"
      :aria-label="ariaLabel || label || undefined"
      @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
    <span v-if="label" class="checkbox__label">{{ label }}</span>
  </label>
</template>

<style scoped>
.checkbox { display: inline-flex; align-items: center; gap: var(--sp-1); cursor: pointer; }
.checkbox.is-disabled { cursor: not-allowed; color: var(--text-muted); }
.checkbox input { accent-color: var(--accent); width: 13px; height: 13px; margin: 0; cursor: inherit; }
.checkbox__label { font-size: var(--fs-ui); }
</style>

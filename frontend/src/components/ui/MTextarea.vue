<script setup lang="ts">
withDefaults(defineProps<{
  modelValue?: string
  label?: string
  placeholder?: string
  error?: string
  disabled?: boolean
  rows?: number
  mono?: boolean
}>(), { modelValue: '', label: '', placeholder: '', error: '', disabled: false, rows: 3, mono: false })

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <label class="field">
    <span v-if="label" class="field__label">{{ label }}</span>
    <textarea class="field__area" :class="{ 'is-error': error, 'is-mono': mono }"
      :value="modelValue" :placeholder="placeholder" :disabled="disabled" :rows="rows"
      :aria-invalid="error ? 'true' : undefined"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" />
    <span v-if="error" class="field__error">{{ error }}</span>
  </label>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; }
.field__label { font-size: var(--fs-ui); color: var(--text-secondary); }
.field__area {
  padding: var(--sp-1) var(--sp-2);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-primary);
  font-size: var(--fs-ui);
  resize: vertical;
  min-width: 0;
}
.field__area::placeholder { color: var(--text-muted); }
.field__area:hover:not(:disabled) { border-color: var(--border-strong); }
.field__area:disabled { color: var(--text-muted); cursor: not-allowed; }
.field__area.is-error { border-color: var(--danger); }
.field__area.is-mono { font-family: var(--font-mono); }
.field__error { font-size: var(--fs-xs); color: var(--danger); }
</style>

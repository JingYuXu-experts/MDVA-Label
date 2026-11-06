<script setup lang="ts">
withDefaults(defineProps<{
  modelValue?: string
  label?: string
  placeholder?: string
  error?: string
  disabled?: boolean
  mono?: boolean
  type?: string
}>(), { modelValue: '', label: '', placeholder: '', error: '', disabled: false, mono: false, type: 'text' })

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <label class="field">
    <span v-if="label" class="field__label">{{ label }}</span>
    <input class="field__input" :class="{ 'is-error': error, 'is-mono': mono }"
      :type="type" :value="modelValue" :placeholder="placeholder" :disabled="disabled"
      :aria-invalid="error ? 'true' : undefined"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)" />
    <span v-if="error" class="field__error">{{ error }}</span>
  </label>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; }
.field__label { font-size: var(--fs-ui); color: var(--text-secondary); }
.field__input {
  height: 28px;
  padding: 0 var(--sp-2);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-primary);
  font-size: var(--fs-ui);
  min-width: 0;
}
.field__input::placeholder { color: var(--text-muted); }
.field__input:hover:not(:disabled) { border-color: var(--border-strong); }
.field__input:disabled { color: var(--text-muted); cursor: not-allowed; }
.field__input.is-error { border-color: var(--danger); }
.field__input.is-mono { font-family: var(--font-mono); }
.field__error { font-size: var(--fs-xs); color: var(--danger); }
</style>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronUp, ChevronDown } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  modelValue?: number
  label?: string
  min?: number
  max?: number
  step?: number
  unit?: string
  error?: string
  disabled?: boolean
}>(), { modelValue: 0, label: '', min: -Infinity, max: Infinity, step: 1, unit: '', error: '', disabled: false })

const emit = defineEmits<{ 'update:modelValue': [value: number] }>()

const clamped = computed(() => Math.min(props.max, Math.max(props.min, props.modelValue)))

function set(value: number) {
  const next = Math.min(props.max, Math.max(props.min, value))
  if (next !== props.modelValue) emit('update:modelValue', next)
}

function onInput(event: Event) {
  const text = (event.target as HTMLInputElement).value
  const parsed = Number(text)
  if (text !== '' && Number.isFinite(parsed)) set(parsed)
}
</script>

<template>
  <label class="field">
    <span v-if="label" class="field__label">{{ label }}</span>
    <span class="numrow">
      <input class="field__input" :class="{ 'is-error': error }" type="number"
        :value="clamped" :min="min === -Infinity ? undefined : min"
        :max="max === Infinity ? undefined : max" :step="step" :disabled="disabled"
        :aria-invalid="error ? 'true' : undefined"
        @input="onInput" />
      <span class="steppers">
        <button type="button" class="step" :disabled="disabled || clamped >= max"
          aria-label="增加" @click="set(clamped + step)"><ChevronUp :size="12" /></button>
        <button type="button" class="step" :disabled="disabled || clamped <= min"
          aria-label="减少" @click="set(clamped - step)"><ChevronDown :size="12" /></button>
      </span>
      <span v-if="unit" class="unit">{{ unit }}</span>
    </span>
    <span v-if="error" class="field__error">{{ error }}</span>
  </label>
</template>

<style scoped>
.field { display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; }
.field__label { font-size: var(--fs-ui); color: var(--text-secondary); }
.numrow { display: inline-flex; align-items: center; gap: var(--sp-1); }
.field__input {
  height: 28px; width: 100%; min-width: 0;
  padding: 0 var(--sp-2);
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-primary);
  font-size: var(--fs-ui);
  font-variant-numeric: tabular-nums;
}
.field__input:hover:not(:disabled) { border-color: var(--border-strong); }
.field__input:disabled { color: var(--text-muted); cursor: not-allowed; }
.field__input.is-error { border-color: var(--danger); }
.steppers { display: inline-flex; flex-direction: column; }
.step {
  display: flex; align-items: center; justify-content: center;
  height: 14px; width: 18px; padding: 0;
  background: var(--bg-raised); border: 1px solid var(--border);
  color: var(--text-secondary); cursor: pointer;
}
.step:first-child { border-radius: var(--radius-control) var(--radius-control) 0 0; }
.step:last-child { border-radius: 0 0 var(--radius-control) var(--radius-control); }
.step:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.step:disabled { color: var(--text-muted); cursor: not-allowed; }
.unit { font-size: var(--fs-xs); color: var(--text-secondary); flex: none; }
.field__error { font-size: var(--fs-xs); color: var(--danger); }
</style>

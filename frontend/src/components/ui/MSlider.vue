<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{
  modelValue: number
  min?: number
  max?: number
  step?: number
  label?: string
  unit?: string
  disabled?: boolean
  format?: (value: number) => string
}>(), { min: 0, max: 100, step: 1, label: '', unit: '', disabled: false })

const emit = defineEmits<{ 'update:modelValue': [value: number] }>()

const dragging = ref(false)
const pct = computed(() => {
  const span = props.max - props.min
  return span > 0 ? ((props.modelValue - props.min) / span) * 100 : 0
})
const shown = computed(() => props.format ? props.format(props.modelValue) : String(props.modelValue))

function onInput(event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (Number.isFinite(value) && value !== props.modelValue) emit('update:modelValue', value)
}
</script>

<template>
  <div class="slider" :class="{ 'is-disabled': disabled }">
    <span v-if="label" class="slider__label">{{ label }}</span>
    <span class="slider__body">
      <input type="range" class="slider__input" :min="min" :max="max" :step="step"
        :value="modelValue" :disabled="disabled"
        :aria-label="label || '滑块'" :aria-valuetext="shown + unit"
        :style="{ '--pct': pct + '%' }"
        @input="onInput" @pointerdown="dragging = true"
        @pointerup="dragging = false" @pointercancel="dragging = false" />
      <span v-if="dragging" class="slider__bubble num" :style="{ left: pct + '%' }">
        {{ shown }}{{ unit }}
      </span>
    </span>
    <span class="slider__value num">{{ shown }}{{ unit }}</span>
  </div>
</template>

<style scoped>
.slider { display: flex; align-items: center; gap: var(--sp-2); min-width: 0; }
.slider.is-disabled { opacity: 0.5; }
.slider__label { font-size: var(--fs-ui); color: var(--text-secondary); flex: none; }
.slider__body { position: relative; flex: 1; min-width: 40px; display: flex; align-items: center; }
.slider__input {
  -webkit-appearance: none; appearance: none;
  width: 100%; height: 14px; background: transparent; cursor: pointer; margin: 0;
}
.slider__input::-webkit-slider-runnable-track {
  height: 3px; border-radius: 2px;
  background: linear-gradient(to right, var(--accent) var(--pct, 0%), var(--border) var(--pct, 0%));
}
.slider__input::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--text-primary); border: 1px solid var(--border-strong);
  margin-top: -4.5px;
}
.slider__input:hover::-webkit-slider-thumb { border-color: var(--accent); }
.slider__input:disabled { cursor: not-allowed; }
.slider__input::-moz-range-track { height: 3px; background: var(--border); border-radius: 2px; }
.slider__input::-moz-range-progress { height: 3px; background: var(--accent); border-radius: 2px; }
.slider__input::-moz-range-thumb {
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--text-primary); border: 1px solid var(--border-strong);
}
.slider__bubble {
  position: absolute; bottom: calc(100% + 2px); transform: translateX(-50%);
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: var(--radius-control); padding: 0 var(--sp-1);
  font-size: var(--fs-xs); color: var(--text-primary); pointer-events: none; white-space: nowrap;
}
.slider__value { font-size: var(--fs-xs); color: var(--text-secondary); flex: none; min-width: 28px; text-align: right; }
</style>

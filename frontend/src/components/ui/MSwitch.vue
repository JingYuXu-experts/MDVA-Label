<script setup lang="ts">
withDefaults(defineProps<{
  modelValue: boolean
  label?: string
  disabled?: boolean
}>(), { modelValue: false, label: '', disabled: false })

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <label class="switch" :class="{ 'is-disabled': disabled }">
    <button type="button" role="switch" class="switch__track" :class="{ 'is-on': modelValue }"
      :aria-checked="modelValue" :aria-label="label || '开关'" :disabled="disabled"
      @click="emit('update:modelValue', !modelValue)">
      <span class="switch__thumb" />
    </button>
    <span v-if="label" class="switch__label">{{ label }}</span>
  </label>
</template>

<style scoped>
.switch { display: inline-flex; align-items: center; gap: var(--sp-2); }
.switch.is-disabled { opacity: 0.5; }
.switch__track {
  position: relative; width: 28px; height: 16px; padding: 0;
  background: var(--bg-raised); border: 1px solid var(--border);
  border-radius: 8px; cursor: pointer; transition: background-color 0.12s;
}
.switch__track.is-on { background: var(--accent); border-color: var(--accent); }
.switch__track:disabled { cursor: not-allowed; }
.switch__thumb {
  position: absolute; top: 2px; left: 2px;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--text-secondary); transition: transform 0.12s, background-color 0.12s;
}
.switch__track.is-on .switch__thumb { transform: translateX(12px); background: #FFFFFF; }
.switch__label { font-size: var(--fs-ui); color: var(--text-primary); }
</style>

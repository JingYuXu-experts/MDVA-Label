<script setup lang="ts" generic="T extends string | number">
import { computed, nextTick, ref, watch } from 'vue'
import { Check, ChevronDown } from 'lucide-vue-next'

export interface SelectOption<V = string> { value: V; label: string; hint?: string }

const props = withDefaults(defineProps<{
  modelValue: T
  options: SelectOption<T>[]
  label?: string
  placeholder?: string
  disabled?: boolean
  width?: string
  error?: string
}>(), { label: '', placeholder: '请选择', disabled: false, width: '', error: '' })

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const open = ref(false)
const filter = ref('')
const activeIdx = ref(0)
const rootEl = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

const searchable = computed(() => props.options.length > 12)
const shown = computed(() => {
  const kw = filter.value.trim().toLowerCase()
  if (!kw) return props.options
  return props.options.filter(o => o.label.toLowerCase().includes(kw))
})
const current = computed(() =>
  props.options.find(o => o.value === props.modelValue) || null)

watch(open, async (value) => {
  if (value) {
    filter.value = ''
    activeIdx.value = Math.max(0, shown.value.findIndex(o => o.value === props.modelValue))
    await nextTick()
    listEl.value?.focus()
    scrollActiveIntoView()
  }
})

function scrollActiveIntoView() {
  const el = listEl.value?.querySelector<HTMLElement>(`[data-idx="${activeIdx.value}"]`)
  el?.scrollIntoView({ block: 'nearest' })
}

function toggle() {
  if (!props.disabled) open.value = !open.value
}

function choose(option: SelectOption<T>) {
  emit('update:modelValue', option.value)
  open.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (!open.value && (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown')) {
    event.preventDefault(); open.value = true; return
  }
  if (!open.value) return
  switch (event.key) {
    case 'Escape': event.preventDefault(); open.value = false; break
    case 'ArrowDown':
      event.preventDefault()
      activeIdx.value = Math.min(shown.value.length - 1, activeIdx.value + 1)
      scrollActiveIntoView(); break
    case 'ArrowUp':
      event.preventDefault()
      activeIdx.value = Math.max(0, activeIdx.value - 1)
      scrollActiveIntoView(); break
    case 'Enter': {
      event.preventDefault()
      const option = shown.value[activeIdx.value]
      if (option) choose(option)
      break
    }
  }
}

// 点击外部关闭
function onDocClick(event: MouseEvent) {
  if (rootEl.value && !rootEl.value.contains(event.target as Node)) open.value = false
}
watch(open, (value) => {
  if (value) document.addEventListener('mousedown', onDocClick)
  else document.removeEventListener('mousedown', onDocClick)
})
</script>

<template>
  <div class="select" :style="width ? { width } : undefined" ref="rootEl">
    <span v-if="label" class="select__label">{{ label }}</span>
    <button type="button" class="select__trigger" :class="{ 'is-open': open, 'is-error': error }"
      :disabled="disabled" :aria-haspopup="'listbox'" :aria-expanded="open"
      :aria-label="label || placeholder" @click="toggle" @keydown="onKeydown">
      <span class="select__value" :class="{ 'is-placeholder': !current }">
        {{ current ? current.label : placeholder }}
      </span>
      <ChevronDown :size="14" class="select__arrow" :class="{ 'is-open': open }" aria-hidden="true" />
    </button>
    <div v-if="open" class="select__pop" role="listbox" tabindex="-1" ref="listEl"
      :aria-label="label || placeholder" @keydown="onKeydown">
      <input v-if="searchable" v-model="filter" class="select__filter" placeholder="过滤…"
        @keydown="onKeydown" @click.stop />
      <ul class="select__list">
        <li v-for="(option, idx) in shown" :key="String(option.value)" :data-idx="idx"
          class="select__option" :class="{ 'is-active': idx === activeIdx, 'is-selected': option.value === modelValue }"
          role="option" :aria-selected="option.value === modelValue"
          @mouseenter="activeIdx = idx" @click.stop="choose(option)">
          <span class="select__option-label">{{ option.label }}</span>
          <span v-if="option.hint" class="select__option-hint">{{ option.hint }}</span>
          <Check v-if="option.value === modelValue" :size="12" class="select__check" aria-hidden="true" />
        </li>
        <li v-if="!shown.length" class="select__empty">无匹配项</li>
      </ul>
    </div>
    <span v-if="error" class="select__error">{{ error }}</span>
  </div>
</template>

<style scoped>
.select { position: relative; display: flex; flex-direction: column; gap: var(--sp-1); min-width: 0; }
.select__label { font-size: var(--fs-ui); color: var(--text-secondary); }
.select__trigger {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-1);
  height: 28px; padding: 0 var(--sp-2);
  background: var(--bg-raised); border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-primary); font-size: var(--fs-ui);
  cursor: pointer; min-width: 0;
}
.select__trigger:hover:not(:disabled) { border-color: var(--border-strong); }
.select__trigger.is-open { border-color: var(--accent); }
.select__trigger.is-error { border-color: var(--danger); }
.select__trigger:disabled { color: var(--text-muted); cursor: not-allowed; }
.select__value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.select__value.is-placeholder { color: var(--text-muted); }
.select__arrow { flex: none; color: var(--text-secondary); transition: transform 0.12s; }
.select__arrow.is-open { transform: rotate(180deg); }
.select__pop {
  position: absolute; top: calc(100% + 2px); left: 0; min-width: 100%; z-index: 60;
  background: var(--bg-raised); border: 1px solid var(--border-strong);
  border-radius: var(--radius-pop); box-shadow: var(--shadow-pop);
  padding: var(--sp-1); max-height: 280px; display: flex; flex-direction: column;
}
.select__filter {
  height: 24px; margin-bottom: var(--sp-1); padding: 0 var(--sp-2);
  background: var(--bg-panel); border: 1px solid var(--border);
  border-radius: var(--radius-control); color: var(--text-primary); font-size: var(--fs-ui);
}
.select__list { list-style: none; margin: 0; padding: 0; overflow-y: auto; min-width: 0; }
.select__option {
  display: flex; align-items: center; gap: var(--sp-2);
  height: 26px; padding: 0 var(--sp-2);
  border-radius: var(--radius-control); cursor: pointer; white-space: nowrap;
}
.select__option.is-active { background: var(--bg-hover); }
.select__option.is-selected { background: var(--accent-soft); color: var(--accent); }
.select__option-label { overflow: hidden; text-overflow: ellipsis; }
.select__option-hint { font-size: var(--fs-xs); color: var(--text-muted); }
.select__check { margin-left: auto; flex: none; }
.select__empty { padding: var(--sp-2); color: var(--text-muted); text-align: center; }
.select__error { font-size: var(--fs-xs); color: var(--danger); }
</style>

<script setup lang="ts">
/**
 * 增强配方的一行算子：勾选 + 参数控件，控件完全由 GET /api/augment/recipes 的 ops.schema 生成，
 * 前端不写死任何算子名或参数名（新增算子零改动上线）。
 *
 * 与清洗 OpItem 的差别只在数组参数：清洗的数组是裁剪框（4 个坐标），
 * 增强的数组是取值集合（如 rotate90 的 k_choices），故这里按"集合勾选"呈现，不当框用。
 */
import { computed } from 'vue'
import MCheckbox from '@/components/ui/MCheckbox.vue'
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSlider from '@/components/ui/MSlider.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import type { AugOpMeta, OpParamSpec } from '@/api/types'
import type { AugOpState } from '@/stores/augment'

const props = defineProps<{ meta: AugOpMeta; state: AugOpState }>()
const emit = defineEmits<{
  toggle: [on: boolean]
  param: [key: string, value: unknown]
}>()

const entries = computed(() => Object.entries(props.meta.schema || {}) as [string, OpParamSpec][])

const isSlider = (s: OpParamSpec) =>
  (s.type === 'number' || s.type === 'integer') && typeof s.min === 'number' && typeof s.max === 'number'
const isNumber = (s: OpParamSpec) => s.type === 'number' || s.type === 'integer'
const isEnum = (s: OpParamSpec) => s.type === 'string' && Array.isArray(s.enum) && s.enum.length > 0
const isArray = (s: OpParamSpec) => s.type === 'array'

/** 集合类数组参数：候选值 = schema 默认 ∪ 当前值（去重升序）；过长就退回文本编辑 */
function choices(key: string, s: OpParamSpec): number[] {
  const cur = Array.isArray(props.state.params[key]) ? (props.state.params[key] as unknown[]) : []
  const def = Array.isArray(s.default) ? s.default : []
  const set = new Set([...def, ...cur].map(Number).filter(n => Number.isFinite(n)))
  return [...set].sort((a, b) => a - b)
}
function setChoices(key: string, value: number, on: boolean) {
  const cur = new Set((Array.isArray(props.state.params[key])
    ? (props.state.params[key] as unknown[]) : []).map(Number))
  if (on) cur.add(value)
  else cur.delete(value)
  emit('param', key, [...cur].sort((a, b) => a - b))
}
function inSet(key: string, value: number): boolean {
  const cur = Array.isArray(props.state.params[key]) ? (props.state.params[key] as unknown[]) : []
  return cur.map(Number).includes(value)
}
/** 集合超过 8 个候选时不再铺勾选，改用逗号文本，避免面板被撑爆 */
const choicesTooLong = (key: string, s: OpParamSpec) => choices(key, s).length > 8
function parseList(raw: string): number[] {
  return raw.split(/[,，\s]+/).map(t => Number(t)).filter(n => Number.isFinite(n))
}

function stepOf(s: OpParamSpec): number {
  if (s.type === 'integer') return 1
  const span = (s.max ?? 1) - (s.min ?? 0)
  const raw = span / 100
  return raw >= 0.01 ? Math.round(raw * 100) / 100 : 0.001
}
function decimalsOf(s: OpParamSpec): number {
  if (s.type === 'integer') return 0
  const st = stepOf(s)
  return st >= 1 ? 0 : Math.min(3, Math.max(2, Math.ceil(-Math.log10(st))))
}
function fmt(s: OpParamSpec) {
  const d = decimalsOf(s)
  return (v: number) => v.toFixed(d)
}
function num(key: string, s: OpParamSpec, v: number) {
  emit('param', key, s.type === 'integer' ? Math.round(v) : v)
}
function enumOptions(s: OpParamSpec) {
  return (s.enum || []).map(v => ({ value: v, label: v }))
}
</script>

<template>
  <div class="oprow" :class="{ 'is-on': state.on }">
    <MCheckbox
      :model-value="state.on"
      :label="meta.name"
      :aria-label="`启用增强算子 ${meta.name}`"
      @update:model-value="emit('toggle', $event)"
    />
    <p v-if="meta.description" class="oprow__desc">{{ meta.description }}</p>

    <div v-if="state.on" class="oprow__params">
      <div v-for="[key, spec] in entries" :key="key" class="param">
        <span class="param__name" :title="spec.description || key">{{ key }}</span>

        <div v-if="isSlider(spec)" class="param__slider">
          <MSlider
            :model-value="Number(state.params[key] ?? spec.default ?? 0)"
            :min="spec.min!"
            :max="spec.max!"
            :step="stepOf(spec)"
            :format="fmt(spec)"
            :aria-label="`${meta.name} ${key}`"
            @update:model-value="num(key, spec, $event)"
          />
          <NumberInput
            :model-value="Number(state.params[key] ?? 0)"
            :min="spec.min"
            :max="spec.max"
            :step="stepOf(spec)"
            :aria-label="`${meta.name} ${key} 精确值`"
            @update:model-value="num(key, spec, $event)"
          />
        </div>

        <MSelect
          v-else-if="isEnum(spec)"
          :model-value="String(state.params[key] ?? spec.default ?? '')"
          :options="enumOptions(spec)"
          :width="'150px'"
          :aria-label="`${meta.name} ${key}`"
          @update:model-value="emit('param', key, $event)"
        />

        <MSwitch
          v-else-if="spec.type === 'boolean'"
          :model-value="Boolean(state.params[key] ?? spec.default)"
          :label="key"
          @update:model-value="emit('param', key, $event)"
        />

        <div v-else-if="isArray(spec) && !choicesTooLong(key, spec)" class="param__set">
          <MCheckbox
            v-for="v in choices(key, spec)"
            :key="v"
            :model-value="inSet(key, v)"
            :label="String(v)"
            :aria-label="`${meta.name} ${key} 取值 ${v}`"
            @update:model-value="setChoices(key, v, $event)"
          />
        </div>

        <MInput
          v-else-if="isArray(spec)"
          :model-value="(Array.isArray(state.params[key]) ? (state.params[key] as unknown[]).map(Number) : []).join(', ')"
          :aria-label="`${meta.name} ${key}（逗号分隔）`"
          @update:model-value="emit('param', key, parseList(String($event)))"
        />

        <NumberInput
          v-else-if="isNumber(spec)"
          :model-value="Number(state.params[key] ?? spec.default ?? 0)"
          :min="spec.min"
          :max="spec.max"
          :step="spec.type === 'integer' ? 1 : 0.01"
          :aria-label="`${meta.name} ${key}`"
          @update:model-value="num(key, spec, $event)"
        />

        <MInput
          v-else
          :model-value="String(state.params[key] ?? spec.default ?? '')"
          :aria-label="`${meta.name} ${key}`"
          @update:model-value="emit('param', key, $event)"
        />

        <span v-if="spec.description" class="param__hint">{{ spec.description }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.oprow { padding: var(--sp-3); border-bottom: 1px solid var(--border); }
.oprow.is-on { background: var(--bg-raised); }
.oprow__desc { margin: 4px 0 0; color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.45; }
.oprow__params { margin-top: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-3); }
.param { display: grid; grid-template-columns: 96px 1fr; gap: 4px var(--sp-2); align-items: center; }
.param__name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.param__slider { display: flex; align-items: center; gap: var(--sp-2); }
.param__slider > :first-child { flex: 1; min-width: 0; }
.param__set { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-3); }
.param__hint { grid-column: 2; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.4; }
</style>

<script setup lang="ts">
/**
 * 单个清洗算子行：勾选 + 参数控件，控件完全由 GET /api/operators?kind=clean 的 schema 生成，
 * 前端不写死任何算子名或参数名（新增算子零改动上线）。
 */
import { computed } from 'vue'
import MCheckbox from '@/components/ui/MCheckbox.vue'
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSlider from '@/components/ui/MSlider.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import type { CleanOpMeta, OpParamSpec } from '@/api/types'
import type { OpState } from '@/stores/clean'

const props = defineProps<{ meta: CleanOpMeta; state: OpState }>()
const emit = defineEmits<{
  toggle: [on: boolean]
  param: [key: string, value: unknown]
}>()

const entries = computed(() => Object.entries(props.meta.schema || {}) as [string, OpParamSpec][])

/** 只有 min 与 max 同时给全才做滑块；否则退化为数字输入，不猜上界 */
const isSlider = (s: OpParamSpec) =>
  (s.type === 'number' || s.type === 'integer') && typeof s.min === 'number' && typeof s.max === 'number'
const isNumber = (s: OpParamSpec) => s.type === 'number' || s.type === 'integer'
const isEnum = (s: OpParamSpec) => s.type === 'string' && Array.isArray(s.enum) && s.enum.length > 0
/** crop 的 box：数组参数按"开关 + 四个数字"呈现，关着就是 null（后端按 center/fraction 居中裁） */
const isBox = (s: OpParamSpec) => s.type === 'array'

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
/** box 类参数：null=未指定（后端居中裁），数组=显式框 */
function boxDefault(s: OpParamSpec): number[] {
  const v = s.default
  return Array.isArray(v) && v.length === 4 ? v.map(Number) : [0, 0, 64, 64]
}
function boxOn(key: string): boolean { return Array.isArray(props.state.params[key]) }
function setBoxPart(key: string, s: OpParamSpec, idx: number, v: number) {
  const cur = Array.isArray(props.state.params[key])
    ? [...(props.state.params[key] as number[])]
    : boxDefault(s)
  cur[idx] = v
  emit('param', key, cur)
}
function toggleBox(key: string, s: OpParamSpec, on: boolean) {
  emit('param', key, on ? boxDefault(s) : null)
}
function enumOptions(s: OpParamSpec) {
  return (s.enum || []).map(v => ({ value: v, label: v }))
}
</script>

<template>
  <div class="opitem" :class="{ 'is-on': state.on }">
    <MCheckbox
      :model-value="state.on"
      :label="meta.name"
      :aria-label="`启用清洗算子 ${meta.name}`"
      @update:model-value="emit('toggle', $event)"
    />
    <p v-if="meta.description" class="opitem__desc">{{ meta.description }}</p>

    <div v-if="state.on" class="opitem__params">
      <div v-for="[key, spec] in entries" :key="key" class="param" :class="{ 'param--wide': isBox(spec) }">
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

        <div v-else-if="isBox(spec)" class="param__box">
          <MSwitch
            :model-value="boxOn(key)"
            label="指定裁剪框"
            @update:model-value="toggleBox(key, spec, $event)"
          />
          <div v-if="boxOn(key)" class="param__boxgrid">
            <NumberInput
              v-for="(lbl, i) in ['x0', 'y0', 'x1', 'y1']"
              :key="lbl"
              :model-value="Number((state.params[key] as number[])?.[i] ?? 0)"
              :min="0"
              :step="1"
              :label="lbl"
              :aria-label="`${meta.name} 裁剪框 ${lbl}`"
              @update:model-value="setBoxPart(key, spec, i, $event)"
            />
          </div>
        </div>

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
.opitem { padding: var(--sp-3); border-bottom: 1px solid var(--border); }
.opitem.is-on { background: var(--bg-raised); }
.opitem__desc { margin: 4px 0 0; color: var(--text-secondary); font-size: var(--fs-xs); line-height: 1.45; }
.opitem__params { margin-top: var(--sp-3); display: flex; flex-direction: column; gap: var(--sp-3); }
.param { display: grid; grid-template-columns: 92px 1fr; gap: 4px var(--sp-2); align-items: center; }
.param--wide { grid-template-columns: 92px 1fr; }
.param__name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.param__slider { display: flex; align-items: center; gap: var(--sp-2); }
.param__slider > :first-child { flex: 1; min-width: 0; }
.param__hint { grid-column: 2; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.4; }
.param__box { display: flex; flex-direction: column; gap: var(--sp-2); }
.param__boxgrid { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-2); }
</style>

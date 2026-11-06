<script setup lang="ts">
/**
 * 导出参数表单：控件由 GET /api/export/formats 的 schema 生成，前端不写死格式名与参数名。
 * `skip` 列出的键交给 Step② 的划分滑杆渲染（比例串与种子），避免同一个参数两处可编辑。
 */
import MInput from '@/components/ui/MInput.vue'
import MSelect from '@/components/ui/MSelect.vue'
import MSlider from '@/components/ui/MSlider.vue'
import MSwitch from '@/components/ui/MSwitch.vue'
import NumberInput from '@/components/ui/NumberInput.vue'
import type { OpParamSpec } from '@/api/types'
import { useExportStore } from '@/stores/export'

const props = defineProps<{ skip?: string[] }>()
const store = useExportStore()

const entries = () => Object.entries(store.schema || {})
  .filter(([k]) => !(props.skip || []).includes(k)) as [string, OpParamSpec][]

const isSlider = (s: OpParamSpec) =>
  (s.type === 'number' || s.type === 'integer') && typeof s.min === 'number' && typeof s.max === 'number'
const isNumber = (s: OpParamSpec) => s.type === 'number' || s.type === 'integer'
const isEnum = (s: OpParamSpec) => s.type === 'string' && Array.isArray(s.enum) && s.enum.length > 0
/** default=null 的参数是"可选项"：留空即不上送，界面必须说清留空是什么意思 */
const isOptional = (s: OpParamSpec) => s.default === null || s.default === undefined

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
  store.setParam(key, s.type === 'integer' ? Math.round(v) : v, String(v))
}
function hint(s: OpParamSpec): string {
  const base = s.description || ''
  if (!isOptional(s)) return base
  if (s.type === 'array') return `${base} · 留空=全部（逗号分隔）`
  if (isNumber(s)) return `${base} · 留空=用后端默认`
  return `${base} · 留空=用后端默认`
}
</script>

<template>
  <div class="pform" data-testid="params-form">
    <div v-for="[key, spec] in entries()" :key="key" class="pform__row">
      <span class="pform__name" :title="spec.description || key">{{ key }}</span>

      <MSelect
        v-if="isEnum(spec)"
        :model-value="String(store.params[key]?.value ?? spec.default ?? '')"
        :options="(spec.enum || []).map(v => ({ value: v, label: v }))"
        :width="'180px'"
        :aria-label="`${store.chosen} ${key}`"
        @update:model-value="store.setParam(key, String($event), String($event))"
      />

      <MSwitch
        v-else-if="spec.type === 'boolean' && !isOptional(spec)"
        :model-value="Boolean(store.params[key]?.value)"
        :label="key"
        @update:model-value="store.setParam(key, Boolean($event), String($event))"
      />

      <MSwitch
        v-else-if="spec.type === 'boolean'"
        :model-value="String(store.params[key]?.text ?? '') === 'true'"
        :label="key"
        @update:model-value="store.setParam(key, Boolean($event), $event ? 'true' : '')"
      />

      <div v-else-if="isSlider(spec)" class="pform__slider">
        <MSlider
          :model-value="Number(store.params[key]?.value ?? spec.default ?? 0)"
          :min="spec.min!"
          :max="spec.max!"
          :step="stepOf(spec)"
          :format="fmt(spec)"
          :aria-label="`${store.chosen} ${key}`"
          @update:model-value="num(key, spec, $event)"
        />
        <NumberInput
          :model-value="Number(store.params[key]?.value ?? 0)"
          :min="spec.min"
          :max="spec.max"
          :step="stepOf(spec)"
          :aria-label="`${store.chosen} ${key} 精确值`"
          @update:model-value="num(key, spec, $event)"
        />
      </div>

      <NumberInput
        v-else-if="isNumber(spec) && !isOptional(spec)"
        :model-value="Number(store.params[key]?.value ?? spec.default ?? 0)"
        :min="spec.min"
        :max="spec.max"
        :step="spec.type === 'integer' ? 1 : 0.01"
        :aria-label="`${store.chosen} ${key}`"
        @update:model-value="num(key, spec, $event)"
      />

      <MInput
        v-else
        :model-value="store.params[key]?.text ?? ''"
        :mono="spec.type !== 'string'"
        :placeholder="isOptional(spec) ? '（留空 = 用默认）' : ''"
        :aria-label="`${store.chosen} ${key}`"
        @update:model-value="store.setParamText(key, String($event))"
      />

      <span v-if="hint(spec)" class="pform__hint">{{ hint(spec) }}</span>
    </div>
    <p v-if="!entries().length" class="pform__none">这种格式没有可调参数，按默认导出即可。</p>
  </div>
</template>

<style scoped>
.pform { display: flex; flex-direction: column; gap: var(--sp-3); }
.pform__row { display: grid; grid-template-columns: 168px minmax(220px, 420px) 1fr; gap: var(--sp-3); align-items: center; }
.pform__name { font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-muted); }
.pform__slider { display: flex; align-items: center; gap: var(--sp-2); }
.pform__slider > :first-child { flex: 1; min-width: 0; }
.pform__hint { font-size: var(--fs-xs); color: var(--text-muted); line-height: 1.4; }
.pform__none { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
</style>

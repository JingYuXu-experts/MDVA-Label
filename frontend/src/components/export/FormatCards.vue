<script setup lang="ts">
/**
 * 格式卡片（§5.5 Step①）：单选，卡片内容全部来自 GET /api/export/formats。
 * 用原生 radio 包在 label 里，键盘方向键与读屏语义由浏览器保证（不用 div 假造单选）。
 */
import MTag from '@/components/ui/MTag.vue'
import type { ExportFormatMeta } from '@/api/types'
import { useExportStore } from '@/stores/export'

const store = useExportStore()

const RATIO_RE = /^\s*0?\.\d+\s*\/\s*0?\.\d+\s*\/\s*0?\.\d+\s*$/
/** 卡片上的"可分折"标记也是结构探测，不写死格式名 */
const splitable = (f: ExportFormatMeta) => Object.values(f.schema || {}).some(
  s => s.type === 'string' && typeof s.default === 'string' && RATIO_RE.test(s.default))
const paramCount = (f: ExportFormatMeta) => Object.keys(f.schema || {}).length
</script>

<template>
  <div class="fmts" role="radiogroup" aria-label="导出格式" data-el="format-cards">
    <label
      v-for="f in store.formats"
      :key="f.name"
      class="fmt"
      :class="{ 'is-chosen': store.chosen === f.name }"
    >
      <input
        class="fmt__radio"
        type="radio"
        name="export-format"
        :value="f.name"
        :checked="store.chosen === f.name"
        @change="store.selectFormat(f.name)"
      >
      <span class="fmt__main">
        <span class="fmt__name">{{ f.name }}</span>
        <span class="fmt__desc">{{ f.description }}</span>
        <span class="fmt__meta">
          <MTag :kind="splitable(f) ? 'accent' : 'neutral'" :text="splitable(f) ? '可选折分比例' : '划分走后端默认'" />
          <span class="fmt__count num">{{ paramCount(f) }} 个参数</span>
        </span>
      </span>
    </label>
    <p v-if="store.formatsLoading" class="fmts__state">格式表读取中…</p>
    <p v-else-if="store.formatsError" class="fmts__state is-err" role="alert">
      格式表读不出来：{{ store.formatsError }}
    </p>
    <p v-else-if="!store.formats.length" class="fmts__state">后端没有返回任何导出格式</p>
  </div>
</template>

<style scoped>
.fmts { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: var(--sp-3); }
.fmt {
  display: flex; gap: var(--sp-3); padding: var(--sp-3); cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-raised);
}
.fmt:hover { border-color: var(--border-strong); background: var(--bg-hover); }
.fmt.is-chosen { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent-soft); }
.fmt__radio { flex: 0 0 auto; margin-top: 3px; accent-color: var(--accent); }
.fmt:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.fmt__main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fmt__name { font-family: var(--font-mono); font-size: var(--fs-md); color: var(--text-primary); font-weight: 600; }
.fmt__desc { font-size: var(--fs-xs); line-height: 1.5; color: var(--text-secondary); }
.fmt__meta { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.fmt__count { font-size: var(--fs-xs); color: var(--text-muted); }
.fmts__state { grid-column: 1 / -1; margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.fmts__state.is-err { color: var(--danger); }
</style>
